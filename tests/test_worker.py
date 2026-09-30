from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.worker import deliver_notifications

def state():
    story=SimpleNamespace(id=1,user_id=10,title='Story',status='active')
    update=SimpleNamespace(id=2,summary='New fact',reason='Official confirmation',source_urls=['https://example.org/news'],
        is_demo=False,delivery_lock_token='lease')
    service=SimpleNamespace(repo=AsyncMock(),_error=AsyncMock())
    service.repo.pending_notifications.return_value=[(update,story)]
    service.repo.get_story.return_value=story
    bot=AsyncMock()
    bot.send_message.return_value=SimpleNamespace(message_id=123)
    return service,bot,story,update

async def test_delivery_claims_one_and_marks_exact_lease_only_after_send():
    service,bot,story,update=state()
    await deliver_notifications(service,bot)
    service.repo.pending_notifications.assert_awaited_once_with(limit=1)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.kwargs['request_timeout']==30
    service.repo.mark_notified.assert_awaited_once_with(2,True,delivery_token='lease',telegram_message_id=123)

async def test_paused_story_never_sends_previously_claimed_notification():
    service,bot,story,update=state()
    story.status='paused'
    await deliver_notifications(service,bot)
    bot.send_message.assert_not_called()
    service.repo.mark_notified.assert_awaited_once_with(2,False,delivery_token='lease',telegram_message_id=None)

async def test_transport_failure_keeps_outbox_retryable():
    service,bot,story,update=state()
    bot.send_message.side_effect=TimeoutError()
    await deliver_notifications(service,bot)
    service.repo.mark_notified.assert_awaited_once_with(2,False,delivery_token='lease',telegram_message_id=None)
    service._error.assert_awaited_once()
