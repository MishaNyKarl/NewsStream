import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.config import Settings
from app.domain import Analysis, Candidate, SearchResult, StoryExtraction, UserError
from app.service import BotService

def story():
    return SimpleNamespace(id=10,user_id=100,title='Product release',original_url=None,
        current_state='No date announced',watch_goals=['Release date'],entities=['Company'],
        keywords=['release'],search_queries=['Company release'],created_at=datetime.now(timezone.utc)-timedelta(days=1),
        lock_token='test-lease')

def analysis(**kwargs):
    fields = dict(relevant=True,meaningful_update=True,novelty_score=.9,importance_score=.9,
        confidence=.9,new_facts=['Release date confirmed'],updated_state='Release on October 10',
        reason='Official release date',notification_summary='The date was announced.',
        source_urls=['https://example.org/release'])
    fields.update(kwargs)
    return Analysis(**fields)

def setup_service():
    repo = AsyncMock()
    repo.known_sources.return_value=[]
    repo.known_url_set.return_value=set()
    repo.recent_updates.return_value=[]
    repo.get_user.return_value=None
    ai = AsyncMock()
    search = AsyncMock()
    fetcher = AsyncMock()
    settings=Settings(_env_file=None,llm_provider='openrouter',llm_model='model',llm_api_key='test-key',invite_code='invite-secret',
                      search_provider='bing_news', search_expand_queries=False, max_search_queries_per_story=3)
    return BotService(settings,repo,ai,search,fetcher)

async def test_unauthorized_cannot_create_or_manually_check():
    service=setup_service()
    with pytest.raises(UserError):
        await service.prepare_story(1,'Please follow product launch')
    with pytest.raises(UserError):
        await service.request_check(1,10)
    service.ai.extract.assert_not_called()
    service.repo.claim_story.assert_not_called()

async def test_invite_requires_exact_secret_and_does_not_create_admin():
    service=setup_service()
    assert await service.authorize(1,start_arg='invite_wrong') is None
    service.repo.admit_user.assert_not_called()
    await service.authorize(1,start_arg='invite_invite-secret')
    assert service.repo.admit_user.call_args.kwargs['is_admin'] is False

async def test_daily_budget_prevents_network_attempt():
    service=setup_service()
    service.repo.reserve_llm_call.return_value=False
    with pytest.raises(UserError,match='лимит'):
        await service._usage('llm_attempt','openrouter')
    service.repo.record_usage.assert_not_called()

async def test_old_and_known_links_are_never_reanalyzed():
    service=setup_service()
    item=story()
    service.repo.known_url_set.return_value={'https://example.org/known'}
    service.search.search.return_value=[
        SearchResult('https://example.org/known','Known article'),
        SearchResult('https://example.org/old','Archive',published_at=item.created_at-timedelta(days=10)),
    ]
    await service.check_story(item)
    service.ai.analyze.assert_not_called()
    service.fetcher.fetch.assert_not_called()
    assert service.repo.save_check.call_args.args[2] == []
    service.repo.finish_check.assert_awaited_once_with(item.id,item.lock_token,error=False)

@pytest.mark.parametrize('change',[
    {'meaningful_update':False}, {'confidence':.70}, {'source_urls':['https://unprovided.org']},
    {'new_facts':[]}, {'relevant':False}, {'novelty_score':.3},
])
async def test_conservative_gate_suppresses_unsupported_notifications(change):
    service=setup_service()
    service._candidates=AsyncMock(return_value=[Candidate('https://example.org/release','https://example.org/release','example.org','Release','Date announced','hash')])
    service.ai.analyze.return_value=analysis(**change)
    await service.check_story(story())
    assert service.repo.save_check.call_args.args[3].meaningful_update is False

async def test_supported_new_fact_reaches_transactional_outbox():
    service=setup_service()
    service._candidates=AsyncMock(return_value=[Candidate('https://example.org/release','https://example.org/release','example.org','Release','Date announced','hash')])
    service.ai.analyze.return_value=analysis()
    await service.check_story(story())
    assert service.repo.save_check.call_args.args[3].meaningful_update is True

async def test_provider_failure_releases_lease_with_backoff_no_source_saved():
    service=setup_service()
    service._candidates=AsyncMock(side_effect=TimeoutError())
    item=story()
    await service.check_story(item)
    service.repo.save_check.assert_not_called()
    service.repo.finish_check.assert_awaited_once_with(item.id,item.lock_token,error=True)

async def test_blocked_url_does_not_reach_ai():
    service=setup_service()
    service.repo.get_user.return_value=SimpleNamespace(telegram_id=100)
    service.fetcher.fetch.side_effect=ValueError('private address')
    with pytest.raises(UserError,match='страницу'):
        await service.prepare_story(100,'http://169.254.169.254/latest/meta-data')
    service.ai.extract.assert_not_called()


async def test_concurrent_manual_admission_never_claims_or_spends_quota_when_full():
    service = setup_service()
    service.repo.get_user.return_value = SimpleNamespace(telegram_id=100)
    release_checks = asyncio.Event()
    quota_spent = []

    async def claim(story_id, user_id, manual):
        # A real DB claim yields. Admission must remain serialized across that await.
        await asyncio.sleep(0)
        quota_spent.append((user_id, story_id))
        return SimpleNamespace(id=story_id, user_id=user_id)

    async def running_check(item):
        await release_checks.wait()

    service.repo.claim_story.side_effect = claim
    service.check_story = AsyncMock(side_effect=running_check)
    try:
        results = await asyncio.gather(
            *(service.request_check(100, story_id) for story_id in (10, 11, 12)),
            return_exceptions=True,
        )
        assert sum(isinstance(result, str) for result in results) == 2
        rejected = [result for result in results if isinstance(result, UserError)]
        assert len(rejected) == 1
        assert 'лимит не потрачен' in str(rejected[0])
        assert service.repo.claim_story.await_count == 2
        assert len(quota_spent) == 2
        assert len(service._tasks) == 2

        with pytest.raises(UserError, match='лимит не потрачен'):
            await service.request_check(100, 13)
        assert service.repo.claim_story.await_count == 2
        assert len(quota_spent) == 2

        # Once checks finish, a fresh request can take a slot normally.
        release_checks.set()
        await asyncio.gather(*tuple(service._tasks))
        await service.request_check(100, 14)
        assert service.repo.claim_story.await_count == 3
        assert quota_spent[-1] == (100, 14)
    finally:
        release_checks.set()
        await service.close()


async def test_repeated_confirmation_persists_one_creation_event(tmp_path):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.models import Base, UsageEvent
    from app.repository import Repository

    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'confirmation.db'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        service = setup_service()
        service.repo = Repository(service.settings, factory)
        await service.repo.admit_user(100)
        draft = await service.repo.create_draft(100, 'Наблюдать за запуском', None, StoryExtraction(
            title='Запуск сервиса', short_summary='Ожидается запуск сервиса.',
            current_state='Дата ещё не объявлена.', entities=['Компания'], keywords=['сервис'],
            search_queries=['сервис дата запуска', 'сервис открытие'], watch_goals=['Дата запуска'],
        ))
        first = await service.confirm_story(100, draft.id)
        repeated = await service.confirm_story(100, draft.id)
        assert first.id == repeated.id == draft.id
        assert repeated.status == 'active'
        async with factory() as session:
            count = await session.scalar(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.story_id == draft.id, UsageEvent.operation == 'story_created',
            ))
        assert count == 1
    finally:
        await engine.dispose()
