"""Run on SQLite by default, or an isolated PostgreSQL schema via
NEWSWATCH_TEST_DATABASE_URL=postgresql+asyncpg://... pytest tests/test_repository.py.
"""
import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.domain import Analysis, Candidate, StoryExtraction, UserError
from app.models import Base, Feedback, Source, Story, StoryUpdate, UsageEvent, User, utcnow
from app.repository import Repository


@pytest_asyncio.fixture
async def store(tmp_path):
    url = os.environ.get("NEWSWATCH_TEST_DATABASE_URL", "")
    admin_engine = None
    schema = None
    if url:
        assert url.startswith("postgresql+asyncpg://"), "Use a PostgreSQL test database"
        schema = "test_" + uuid4().hex
        admin_engine = create_async_engine(url)
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    else:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "repository.db"))
        @event.listens_for(engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(_env_file=None, max_testers=2, max_stories_per_user=3,
        max_manual_checks_per_day=2, manual_check_cooldown_seconds=0, llm_daily_call_limit=2)
    repo = Repository(settings, factory)
    try:
        yield repo, factory, settings
    finally:
        await engine.dispose()
        if admin_engine is not None:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()


def extraction():
    return StoryExtraction(title="Новый запуск", short_summary="Ожидается запуск сервиса.",
        current_state="Дата запуска неизвестна.", entities=["Компания"], keywords=["сервис"],
        search_queries=["сервис запуск", "сервис дата"], watch_goals=["Дата запуска"])


def candidate(number=1):
    return Candidate(url=f"https://example.org/{number}", normalized_url=f"https://example.org/{number}",
        domain="example.org", title="Объявлена дата", content_excerpt=f"Дата запуска {number} ноября.",
        content_hash=f"{number:064x}", search_query="сервис запуск")


def analysis(meaningful=True):
    return Analysis(relevant=True, meaningful_update=meaningful, novelty_score=0.9,
        importance_score=0.8, confidence=0.9, new_facts=["Дата запуска — 1 ноября."],
        updated_state="Дата запуска — 1 ноября.", reason="Дата стала известна.",
        notification_summary="Объявлена дата запуска.", source_urls=[candidate().url])


async def active(repo, user_id=1):
    await repo.admit_user(user_id)
    draft = await repo.create_draft(user_id, "Следить за датой", None, extraction())
    return await repo.activate_story(user_id, draft.id)


@pytest.mark.asyncio
async def test_atomic_admission_and_one_time_admin_claim(store):
    repo, factory, settings = store
    peers = [Repository(settings, factory) for _ in range(8)]
    users = await asyncio.gather(*(peer.admit_user(i + 1) for i, peer in enumerate(peers)))
    assert sum(user is not None for user in users) == 2
    admins = await asyncio.gather(*(peer.claim_admin(i + 100) for i, peer in enumerate(peers)))
    assert sum(user is not None for user in admins) == 1
    winner = next(user for user in admins if user)
    assert winner.is_admin
    assert await repo.claim_admin(winner.telegram_id) is None
    assert (await repo.admit_user(999, is_admin=True)).is_admin
    assert await repo.admit_user(998) is None


@pytest.mark.asyncio
async def test_draft_quota_expiry_ownership_and_utc(store):
    repo, factory, _ = store
    await repo.admit_user(1)
    drafts = await asyncio.gather(*(repo.create_draft(1, "Тема", None, extraction()) for _ in range(3)))
    assert drafts[0].created_at.tzinfo is not None
    with pytest.raises(UserError):
        await repo.create_draft(1, "Тема", None, extraction())
    assert await repo.list_stories(1) == []
    assert await repo.get_story(2, drafts[0].id) is None
    with pytest.raises(UserError):
        await repo.activate_story(2, drafts[0].id)
    async with factory.begin() as session:
        old = await session.get(Story, drafts[0].id)
        old.created_at = utcnow() - timedelta(days=2)
    new = await repo.create_draft(1, "Тема", None, extraction())
    assert new.id != drafts[0].id
    assert await repo.get_story(1, drafts[0].id) is None


@pytest.mark.asyncio
async def test_concurrent_story_limit(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    results = await asyncio.gather(*(Repository(settings, factory).create_draft(
        1, "Тема", None, extraction()) for _ in range(8)), return_exceptions=True)
    assert sum(isinstance(result, Story) for result in results) == 3
    assert sum(isinstance(result, UserError) for result in results) == 5


@pytest.mark.asyncio
async def test_claim_exclusion_stale_tokens_and_due_schedule(store):
    repo, factory, settings = store
    story = await active(repo)
    assert story.id in await repo.due_story_ids()
    claims = await asyncio.gather(*(Repository(settings, factory).claim_story(story.id) for _ in range(8)))
    assert sum(item is not None for item in claims) == 1
    first = next(item for item in claims if item)
    assert await repo.due_story_ids() == []
    assert await repo.claim_story(story.id, user_id=2, manual=True) is None
    await repo.finish_check(story.id, "wrong")
    assert (await repo.get_story(1, story.id)).lock_token == first.lock_token
    async with factory.begin() as session:
        row = await session.get(Story, story.id)
        row.lock_until = utcnow() - timedelta(seconds=1)
    second = await repo.claim_story(story.id)
    assert second.lock_token != first.lock_token
    assert await repo.save_check(story.id, first.lock_token, [candidate()], analysis()) is None
    await repo.finish_check(story.id, first.lock_token)
    assert (await repo.get_story(1, story.id)).lock_token == second.lock_token
    await repo.finish_check(story.id, second.lock_token)
    finished = await repo.get_story(1, story.id)
    assert finished.last_checked_at is not None and finished.lock_token is None
    assert finished.next_check_at > utcnow() + timedelta(hours=23)
    assert await repo.claim_story(story.id) is None


@pytest.mark.asyncio
async def test_manual_quota_is_per_user_atomic_and_survives_deletion(store):
    repo, factory, settings = store
    stories = [await active(repo) for _ in range(3)]
    results = await asyncio.gather(*(Repository(settings, factory).claim_story(
        story.id, user_id=1, manual=True) for story in stories), return_exceptions=True)
    assert sum(isinstance(result, Story) for result in results) == 2
    assert sum(isinstance(result, UserError) for result in results) == 1
    claimed = [result for result in results if isinstance(result, Story)]
    for claim in claimed:
        await repo.set_status(1, claim.id, "deleted")
    remaining = next(story for story in stories if story.id not in {item.id for item in claimed})
    with pytest.raises(UserError):
        await repo.claim_story(remaining.id, user_id=1, manual=True)
    automatic = await repo.claim_story(remaining.id)
    assert automatic is not None  # Manual quota must not block automatic checks.
    async with factory() as session:
        user = await session.get(User, 1)
        assert user.manual_checks_today == 2


@pytest.mark.asyncio
async def test_manual_cooldown_applies_to_different_stories(store):
    repo, _, settings = store
    settings.manual_check_cooldown_seconds = 120
    first, second = await active(repo), await active(repo)
    assert await repo.claim_story(first.id, user_id=1, manual=True)
    with pytest.raises(UserError, match="Подождите"):
        await repo.claim_story(second.id, user_id=1, manual=True)


@pytest.mark.asyncio
async def test_silence_atomic_baseline_and_duplicate_idempotency(store):
    repo, _, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    assert await repo.save_check(story.id, claim.lock_token, [candidate(2)], analysis(False)) is None
    assert (await repo.get_story(1, story.id)).current_state == story.current_state
    assert await repo.pending_notifications() == []
    result = await repo.save_check(story.id, claim.lock_token, [candidate()], analysis())
    assert result.previous_state == story.current_state
    assert result.new_state == analysis().updated_state
    assert (await repo.get_story(1, story.id)).current_state == analysis().updated_state
    assert await repo.save_check(story.id, claim.lock_token, [candidate()], analysis()) is None
    assert len(await repo.recent_updates(1, story.id)) == 1
    assert len(await repo.known_sources(story.id)) == 2
    assert await repo.known_url_set(story.id) == {candidate().normalized_url, candidate(2).normalized_url}
    assert await repo.recent_updates(2, story.id) == []


@pytest.mark.asyncio
async def test_pause_revokes_lease_and_prevents_outbox(store):
    repo, _, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    await repo.create_demo(1, story.id)
    assert await repo.set_status(2, story.id, "paused") is None
    await repo.set_status(1, story.id, "paused")
    assert await repo.save_check(story.id, claim.lock_token, [candidate()], analysis()) is None
    assert await repo.claim_story(story.id, 1, manual=True) is None
    assert await repo.pending_notifications() == []
    await repo.finish_check(story.id, claim.lock_token)
    resumed = await repo.set_status(1, story.id, "active")
    assert resumed.next_check_at <= utcnow()
    assert len(await repo.pending_notifications()) == 1


@pytest.mark.asyncio
async def test_outbox_claim_retry_token_and_delivery_limit(store):
    repo, factory, settings = store
    story = await active(repo)
    demo = await repo.create_demo(1, story.id)
    assert (await repo.get_story(1, story.id)).current_state == story.current_state
    peers = [Repository(settings, factory) for _ in range(5)]
    claims = await asyncio.gather(*(peer.pending_notifications() for peer in peers))
    assert sum(len(items) for items in claims) == 1
    first = next(items[0][0] for items in claims if items)
    assert first.is_demo
    await repo.mark_notified(demo.id, True, delivery_token="wrong")
    assert await repo.pending_notifications() == []
    async with factory.begin() as session:
        item = await session.get(StoryUpdate, demo.id)
        item.delivery_locked_until = utcnow() - timedelta(seconds=1)
    second, _ = (await repo.pending_notifications())[0]
    await repo.mark_notified(demo.id, True, delivery_token=first.delivery_lock_token)
    assert (await repo.recent_updates(1, story.id))[0].notified_at is None
    await repo.mark_notified(demo.id, False, delivery_token=second.delivery_lock_token)
    assert await repo.pending_notifications() == []  # Persistent retry backoff.
    async with factory.begin() as session:
        item = await session.get(StoryUpdate, demo.id)
        item.delivery_locked_until = utcnow() - timedelta(seconds=1)
    third, _ = (await repo.pending_notifications())[0]
    await repo.mark_notified(demo.id, True, delivery_token=third.delivery_lock_token)
    assert (await repo.recent_updates(1, story.id))[0].notified_at is not None
    assert await repo.pending_notifications() == []
    exhausted = await repo.create_demo(1, story.id)
    async with factory.begin() as session:
        item = await session.get(StoryUpdate, exhausted.id)
        item.delivery_attempts = 5
    assert await repo.pending_notifications() == []


@pytest.mark.asyncio
async def test_feedback_upsert_ownership_and_delete_redaction(store):
    repo, factory, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    item = await repo.save_check(story.id, claim.lock_token, [candidate()], analysis())
    assert not await repo.feedback(2, item.id, "useful")
    assert not await repo.feedback(1, item.id, "invalid")
    assert await repo.feedback(1, item.id, "useful")
    assert await repo.feedback(1, item.id, "not_useful")
    stats = await repo.admin_stats()
    assert stats["feedback_useful"] == 0 and stats["feedback_not_useful"] == 1
    await repo.record_usage("analyze", user_id=1, story_id=story.id, detail="Sensitive text", estimated_cost=0.25)
    deleted = await repo.set_status(1, story.id, "deleted")
    assert deleted.original_input == deleted.current_state == deleted.summary == ""
    assert deleted.entities == [] and deleted.original_url is None
    await repo.record_usage("late_error", user_id=1, story_id=story.id, detail="Late sensitive text")
    assert await repo.reserve_llm_call(user_id=1, story_id=story.id)
    assert await repo.get_story(1, story.id) is None
    assert await repo.list_stories(1) == []
    async with factory() as session:
        for model in (Source, StoryUpdate, Feedback):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        for operation in ("analyze", "late_error", "llm_reserved"):
            usage = await session.scalar(select(UsageEvent).where(UsageEvent.operation == operation))
            assert usage.user_id is None and usage.story_id is None and usage.detail is None
    assert (await repo.admin_stats())["estimated_cost_total"] == 0.25


@pytest.mark.asyncio
async def test_global_llm_budget_atomic_across_repository_instances(store):
    repo, factory, settings = store
    results = await asyncio.gather(*(Repository(settings, factory).reserve_llm_call() for _ in range(12)))
    assert sum(results) == 2
    assert await repo.usage_count_today("llm_reserved") == 2
    assert (await repo.admin_stats())["llm_calls_today"] == 2


@pytest.mark.asyncio
async def test_error_backoff_and_utc_daily_reset(store):
    repo, factory, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id, 1, manual=True)
    await repo.finish_check(story.id, claim.lock_token, error=True)
    result = await repo.get_story(1, story.id)
    assert result.last_checked_at is None
    assert utcnow() + timedelta(minutes=29) < result.next_check_at < utcnow() + timedelta(minutes=31)
    async with factory.begin() as session:
        user = await session.get(User, 1)
        user.manual_checks_today = 999
        user.manual_quota_day = utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    assert await repo.claim_story(story.id, 1, manual=True)


def _synchronized_repo(factory, settings, locked_model, barrier):
    """Hold each real PostgreSQL row lock until the competing lock is held too.

    Only scheduling is instrumented: the statements and transaction bodies are
    the actual repository methods, including their foreign-key usage inserts.
    """
    class SynchronizedSession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            result = await super().scalar(statement, *args, **kwargs)
            if (isinstance(result, locked_model) and statement._for_update_arg is not None
                and not getattr(self, "_test_lock_synchronized", False)):
                self._test_lock_synchronized = True
                await barrier.wait()
            return result
    sessions = async_sessionmaker(factory.kw["bind"], class_=SynchronizedSession, expire_on_commit=False)
    return Repository(settings, sessions)


@pytest.mark.skipif(not os.environ.get("NEWSWATCH_TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
@pytest.mark.asyncio
async def test_postgresql_manual_claim_and_finish_do_not_deadlock_on_usage_fk(store):
    repo, factory, settings = store
    story = await active(repo)
    initial = await repo.claim_story(story.id)
    barrier = asyncio.Barrier(2)
    claimant = _synchronized_repo(factory, settings, User, barrier)
    finisher = _synchronized_repo(factory, settings, Story, barrier)
    _, manual = await asyncio.wait_for(asyncio.gather(
        finisher.finish_check(story.id, initial.lock_token),
        claimant.claim_story(story.id, user_id=1, manual=True)), timeout=10)
    assert manual is not None and manual.lock_token != initial.lock_token
    assert await repo.usage_count_today("story_check_completed") == 1
    assert await repo.usage_count_today("manual_check_requested") == 1


@pytest.mark.skipif(not os.environ.get("NEWSWATCH_TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
@pytest.mark.asyncio
async def test_postgresql_delivery_ack_and_delete_do_not_deadlock_on_usage_fk(store):
    repo, factory, settings = store
    story = await active(repo)
    await repo.create_demo(1, story.id)
    item, _ = (await repo.pending_notifications())[0]
    barrier = asyncio.Barrier(2)
    acknowledger = _synchronized_repo(factory, settings, StoryUpdate, barrier)
    deleter = _synchronized_repo(factory, settings, Story, barrier)
    _, deleted = await asyncio.wait_for(asyncio.gather(
        acknowledger.mark_notified(item.id, True, delivery_token=item.delivery_lock_token),
        deleter.set_status(1, story.id, "deleted")), timeout=10)
    assert deleted.status == "deleted"
    assert await repo.get_story(1, story.id) is None
    assert await repo.recent_updates(1, story.id) == []
    async with factory() as session:
        event_row = await session.scalar(select(UsageEvent).where(UsageEvent.operation == "notification_sent"))
        assert event_row is not None
        assert event_row.user_id is None and event_row.story_id is None and event_row.detail is None
