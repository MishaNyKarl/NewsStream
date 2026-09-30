"""Cadence, transactional quotas and real Telegram callback paths."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot import parse_transfer, story_keyboard, story_text
from app.domain import UserError
from app.models import Story, UsageEvent
from app.monitoring import IntensiveSlotOccupied, next_checkpoint
from app.repository import Repository
from test_bot import Harness, story as ui_story
from test_repository import active, extraction
from test_repository import store as store

START = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    times = [START]
    monkeypatch.setattr("app.repository.utcnow", lambda: times[0])
    return times


async def draft(repo, user=1):
    await repo.admit_user(user)
    return await repo.create_draft(user, "Сюжет для наблюдения", None, extraction())


def test_checkpoint_boundaries_and_downtime():
    assert next_checkpoint(START, START) == START + timedelta(minutes=30)
    assert next_checkpoint(START, START + timedelta(minutes=30)) == START + timedelta(hours=1)
    assert next_checkpoint(START, START + timedelta(hours=7)) == START + timedelta(hours=8)
    assert next_checkpoint(START, START + timedelta(hours=24)) is None


async def test_intensive_full_day_schedule_then_daily(store, clock):
    repo, _, _ = store
    item = await draft(repo)
    enabled = await repo.set_monitoring_mode(1, item.id, "intensive")
    assert enabled.status == "active"
    assert enabled.next_check_at == START + timedelta(minutes=30)
    assert await repo.claim_story(item.id) is None
    for minutes in (30, 60, 120, 240, 480, 720, 1440):
        clock[0] = START + timedelta(minutes=minutes)
        assert await repo.due_story_ids() == [item.id]
        claim = await repo.claim_story(item.id)
        assert claim is not None
        await repo.finish_check(item.id, claim.lock_token)
    final = await repo.get_story(1, item.id)
    assert final.monitoring_mode == "daily"
    assert final.next_check_at == START + timedelta(hours=48)


async def test_competing_workers_enforce_one_topic_and_idempotency(store, clock):
    repo, factory, settings = store
    a, b = await draft(repo), await draft(repo)
    other = Repository(settings, factory)
    results = await asyncio.gather(repo.set_monitoring_mode(1, a.id, "intensive"),
        other.set_monitoring_mode(1, b.id, "intensive"), return_exceptions=True)
    assert sum(isinstance(item, IntensiveSlotOccupied) for item in results) == 1
    winner = next(item for item in results if isinstance(item, Story))
    clock[0] += timedelta(minutes=10)
    repeated = await other.set_monitoring_mode(1, winner.id, "intensive")
    assert repeated.intensive_started_at == START
    async with factory() as session:
        operations = (await session.scalars(select(UsageEvent.operation))).all()
        assert operations.count("intensive_enabled") == operations.count("story_created") == 1


async def test_transfer_is_atomic_and_stale_confirmation_cannot_replace_new_owner(store, clock):
    repo, _, _ = store
    a, b, c = await draft(repo), await draft(repo), await draft(repo)
    await repo.set_monitoring_mode(1, a.id, "intensive")
    await repo.set_monitoring_mode(1, b.id, "intensive", replace_story_id=a.id)
    with pytest.raises(IntensiveSlotOccupied) as caught:
        await repo.set_monitoring_mode(1, c.id, "intensive", replace_story_id=a.id)
    assert caught.value.story_id == b.id
    assert (await repo.get_story(1, a.id)).monitoring_mode == "daily"
    assert (await repo.get_story(1, c.id)).status == "draft"


async def test_owner_is_required_and_quota_is_per_user(store, clock):
    repo, _, _ = store
    a, b = await draft(repo, 1), await draft(repo, 2)
    with pytest.raises(UserError):
        await repo.set_monitoring_mode(2, a.id, "intensive")
    await repo.set_monitoring_mode(1, a.id, "intensive")
    await repo.set_monitoring_mode(2, b.id, "intensive", replace_story_id=a.id)
    assert (await repo.get_story(1, a.id)).monitoring_mode == "intensive"


async def test_database_constraint_catches_bypassed_quota(store, clock):
    repo, factory, _ = store
    a, b = await active(repo), await active(repo)
    await repo.set_monitoring_mode(1, a.id, "intensive")
    with pytest.raises(IntegrityError):
        async with factory.begin() as session:
            item = await session.get(Story, b.id)
            item.monitoring_mode = "intensive"
            item.intensive_started_at = START
            item.intensive_until = START + timedelta(hours=24)


async def test_pause_retains_slot_and_resume_does_not_restart_window(store, clock):
    repo, _, _ = store
    a, b = await draft(repo), await draft(repo)
    await repo.set_monitoring_mode(1, a.id, "intensive")
    await repo.set_status(1, a.id, "paused")
    clock[0] += timedelta(hours=3)
    assert await repo.due_story_ids() == []
    with pytest.raises(IntensiveSlotOccupied):
        await repo.set_monitoring_mode(1, b.id, "intensive")
    resumed = await repo.set_status(1, a.id, "active")
    assert resumed.next_check_at == START + timedelta(hours=4)
    assert resumed.intensive_started_at == START


@pytest.mark.parametrize("release", ["daily", "delete", "expire", "paused_expire"])
async def test_slot_release_paths(store, clock, release):
    repo, _, _ = store
    a, b = await draft(repo), await draft(repo)
    await repo.set_monitoring_mode(1, a.id, "intensive")
    if release == "daily":
        await repo.set_monitoring_mode(1, a.id, "daily")
    elif release == "delete":
        await repo.set_status(1, a.id, "deleted")
    else:
        if release == "paused_expire":
            await repo.set_status(1, a.id, "paused")
        clock[0] += timedelta(hours=25)
    enabled = await repo.set_monitoring_mode(1, b.id, "intensive")
    assert enabled.monitoring_mode == "intensive"


@pytest.mark.parametrize("mode", ["daily", "intensive"])
@pytest.mark.parametrize("error", [False, True])
async def test_early_manual_check_never_postpones_schedule(store, clock, mode, error):
    repo, _, _ = store
    item = await active(repo)
    if mode == "intensive":
        item = await repo.set_monitoring_mode(1, item.id, mode)
    else:
        claim = await repo.claim_story(item.id)
        await repo.finish_check(item.id, claim.lock_token)
        item = await repo.get_story(1, item.id)
    scheduled = item.next_check_at
    clock[0] += timedelta(minutes=5)
    claim = await repo.claim_story(item.id, 1, manual=True)
    await repo.finish_check(item.id, claim.lock_token, error=error)
    assert (await repo.get_story(1, item.id)).next_check_at == scheduled


async def test_downtime_failure_retry_and_mode_change_during_check(store, clock):
    repo, _, _ = store
    item = await draft(repo)
    await repo.set_monitoring_mode(1, item.id, "intensive")
    clock[0] += timedelta(hours=3)
    claim = await repo.claim_story(item.id)
    await repo.finish_check(item.id, claim.lock_token, error=True)
    assert (await repo.get_story(1, item.id)).next_check_at == clock[0] + timedelta(minutes=5)
    clock[0] += timedelta(minutes=5)
    claim = await repo.claim_story(item.id)
    changed = await repo.set_monitoring_mode(1, item.id, "daily")
    await repo.finish_check(item.id, claim.lock_token)
    assert (await repo.get_story(1, item.id)).next_check_at == changed.next_check_at


def test_card_explains_single_slot_pause_and_next_check():
    item = ui_story(monitoring_mode="intensive", intensive_until=START + timedelta(hours=24), next_check_at=START)
    text = story_text(item)
    assert "одна такая тема" in text and "30 мин, 1, 2, 4, 8, 12 и 24" in text
    assert "Ближайшая проверка" in text and "МСК" in text
    item.status = "paused"
    assert "Место занято" in story_text(item)
    assert "Ближайшая проверка" not in story_text(item)
    assert any(button.callback_data == "daily:11" for row in story_keyboard(item).inline_keyboard for button in row)


@pytest.mark.parametrize("value", [None, "transfer:0:1", "transfer:1:1", "transfer:1:-2", "transfer:1:2147483648", "transfer:1:2:3"])
def test_transfer_payload_validation(value):
    assert parse_transfer(value) is None


async def test_telegram_enable_and_transfer_confirmation():
    from unittest.mock import AsyncMock
    harness = Harness()
    focused = ui_story(monitoring_mode="intensive", intensive_until=START + timedelta(hours=24), next_check_at=START)
    harness.service.set_monitoring_mode = AsyncMock(side_effect=[IntensiveSlotOccupied(12, "<Old>"), focused])
    await harness.callback("focus:11")
    assert "&lt;Old&gt;" in harness.text and "Перенести" in harness.text
    assert "transfer:11:12" in str(harness.session.calls)
    harness.reset_throttle()
    await harness.callback("transfer:11:12")
    harness.service.set_monitoring_mode.assert_awaited_with(100, 11, "intensive", replace_story_id=12)
    assert "включён" in harness.text


async def test_telegram_preview_and_declining_transfer_preserve_draft():
    from unittest.mock import AsyncMock
    harness = Harness()
    harness.service.get_story.return_value = ui_story(status="draft")
    harness.service.set_monitoring_mode = AsyncMock()
    await harness.callback("card:11")
    assert "одна такая тема" in harness.text
    assert "focus:11" in str(harness.session.calls)
    harness.service.set_monitoring_mode.assert_not_awaited()
