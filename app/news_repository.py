"""User-owned submitted news and a durable queue for ready notifications."""
from datetime import timedelta
import hashlib
import unicodedata
from uuid import uuid4

from sqlalchemy import delete, func, or_, select

from app.domain import InterestSaveResult, StoryExtraction, UserError
from app.models import Story, User, UserInterest, UserNews, utcnow
from app.product_analytics import add_event


class NewsRepository:
    async def save_user_news(self, user_id, text, source_url=None, use_text=False, input_message_id=None):
        async with self._transaction() as db:
            user = await db.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            if user is None:
                raise UserError('Откройте доступ через /start.')
            if input_message_id is not None:
                existing = await db.scalar(select(UserNews).where(
                    UserNews.user_id == user_id, UserNews.input_message_id == input_message_id))
                if existing:
                    return existing
            item = UserNews(user_id=user_id, original_text=text, source_url=source_url, use_text=use_text,
                            input_message_id=input_message_id, status='pending')
            db.add(item)
            await db.flush()
            add_event(db, 'news_submitted', user_id)
            return item

    async def list_user_news(self, user_id, before_id=0):
        async with self._transaction() as db:
            query = select(UserNews).where(UserNews.user_id == user_id)
            if before_id:
                query = query.where(UserNews.id < before_id)
            return list((await db.scalars(query.order_by(UserNews.id.desc()).limit(9))).all())

    async def get_user_news(self, user_id, news_id):
        async with self._transaction() as db:
            return await db.scalar(select(UserNews).where(UserNews.user_id == user_id, UserNews.id == news_id))

    async def claim_user_news(self, user_id, news_id):
        async with self._transaction() as db:
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True))
            if item is None:
                raise UserError('Новость удалена или недоступна.')
            if item.status == 'ready':
                return item
            if item.processing_until and item.processing_until > utcnow():
                raise UserError('Эта новость уже обрабатывается. Готовность появится в «Новости пользователя».')
            item.status = 'processing'
            item.processing_token = uuid4().hex
            item.processing_until = utcnow() + timedelta(minutes=6)
            item.error_message = None
            item.updated_at = utcnow()
            return item

    async def finish_user_news(self, user_id, news_id, token, extraction=None, source_url=None, error=None):
        async with self._transaction() as db:
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True))
            if (item is None or item.status != 'processing' or item.processing_token != token
                    or item.processing_until is None or item.processing_until <= utcnow()):
                raise UserError('Обработка прервана или новость удалена. Откройте «Новости пользователя».')
            item.processing_token = item.processing_until = None
            item.updated_at = utcnow()
            if extraction is not None:
                item.parsed_data = extraction.model_dump()
                item.source_url = source_url or item.source_url
                item.status = 'ready'
                add_event(db, 'news_ready', user_id)
            else:
                item.status = 'failed'
                add_event(db, 'news_failed', user_id)
                item.error_message = (error or 'Не удалось завершить обработку. Можно повторить.')[:1200]
            return item

    async def user_news_story(self, user_id, news_id):
        # Lock order: user -> existing story -> news, matching story mutations.
        async with self._transaction() as db:
            await db.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            expired = (await db.scalars(select(Story).where(Story.user_id == user_id, Story.status == 'draft',
                Story.created_at < utcnow() - timedelta(hours=24)).order_by(Story.id).with_for_update(key_share=True))).all()
            for old in expired:
                await self._redact(db, old)
            await db.flush()
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id, UserNews.id == news_id))
            if item is None or item.status != 'ready' or not item.parsed_data:
                raise UserError('Сначала дождитесь обработки новости или повторите её из списка.')
            existing = await self._owned(db, user_id, item.story_id) if item.story_id else None
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True).execution_options(populate_existing=True))
            if item is None:
                raise UserError('Новость удалена.')
            if existing:
                return existing
            count = await db.scalar(select(func.count()).select_from(Story).where(
                Story.user_id == user_id, Story.status != 'deleted'))
            if count >= self.settings.max_stories_per_user:
                raise UserError('Лимит наблюдений достигнут. Освободите место; сама новость сохранена в «Новости пользователя».')
            parsed = StoryExtraction.model_validate(item.parsed_data)
            story = Story(user_id=user_id, title=parsed.title, original_input=item.original_text,
                original_url=item.source_url, summary=parsed.short_summary, current_state=parsed.current_state,
                entities=parsed.entities, keywords=parsed.keywords, search_queries=parsed.search_queries,
                watch_goals=parsed.watch_goals, status='draft',
                check_frequency_hours=self.settings.default_check_interval_hours)
            db.add(story)
            await db.flush([story])
            item.story_id = story.id
            return story

    async def user_news_interest(self, user_id, news_id):
        # Saving an interest must also work when the monitoring quota is full.
        async with self._transaction() as db:
            await db.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id, UserNews.id == news_id))
            if item is None or item.status != 'ready' or not item.parsed_data:
                raise UserError('Новость удалена или ещё не обработана.')
            story = await self._owned(db, user_id, item.story_id) if item.story_id else None
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True).execution_options(populate_existing=True))
            if item is None:
                raise UserError('Новость удалена.')
            normalized = ' '.join(unicodedata.normalize('NFKC', item.original_text).casefold().split())
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
            match = UserInterest.input_fingerprint == fingerprint
            if item.story_id:
                match = or_(match, UserInterest.source_story_id == item.story_id)
            existing = await db.scalar(select(UserInterest).where(UserInterest.user_id == user_id, match))
            item.notice_suppressed = True
            status = story.status if story else 'draft'
            if existing is not None:
                if story and story.status == 'draft':
                    await self._redact(db, story)
                    status = 'deleted'
                return InterestSaveResult(existing, False, status)
            data = StoryExtraction.model_validate(item.parsed_data)
            interest = UserInterest(user_id=user_id, source_story_id=story.id if story else None,
                title=data.title, summary=data.short_summary, entities=list(data.entities),
                keywords=list(data.keywords), source_url=item.source_url, input_fingerprint=fingerprint)
            db.add(interest)
            await db.flush()
            if story and story.status == 'draft':
                await self._redact(db, story)
                status = 'deleted'
            self._event(db, 'interest_saved', user_id=user_id)
            return InterestSaveResult(interest, True, status)

    async def defer_user_news(self, user_id, news_id, reason='user'):
        async with self._transaction() as db:
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True))
            if item is None:
                raise UserError('Новость удалена или недоступна.')
            if not item.notice_suppressed and reason == 'user':
                add_event(db, 'news_deferred', user_id)
            item.notice_suppressed = True

    async def delete_user_news(self, user_id, news_id):
        async with self._transaction() as db:
            await db.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            removed = await db.execute(delete(UserNews).where(UserNews.user_id == user_id, UserNews.id == news_id))
            if removed.rowcount:
                add_event(db, 'news_deleted', user_id)

    async def pending_news_notices(self):
        async with self._transaction() as db:
            now = utcnow()
            items = list((await db.scalars(select(UserNews).where(UserNews.status == 'ready',
                UserNews.notice_suppressed.is_(False), UserNews.notice_sent_at.is_(None), UserNews.notice_attempts < 5,
                or_(UserNews.notice_locked_until.is_(None), UserNews.notice_locked_until <= now))
                .order_by(UserNews.id).limit(1).with_for_update(skip_locked=True, key_share=True))).all())
            for item in items:
                item.notice_token = uuid4().hex
                item.notice_locked_until = now + timedelta(minutes=2)
                item.notice_attempts += 1
            return items

    async def mark_news_notice(self, user_id, news_id, token, success, retry_after=0):
        async with self._transaction() as db:
            item = await db.scalar(select(UserNews).where(UserNews.user_id == user_id,
                UserNews.id == news_id).with_for_update(key_share=True))
            if (item is None or item.notice_token != token or item.notice_locked_until is None
                    or item.notice_locked_until <= utcnow() or item.notice_sent_at is not None):
                return
            item.notice_token = None
            if success:
                item.notice_sent_at = utcnow()
                item.notice_locked_until = None
                add_event(db, 'ready_notice_sent', user_id)
            else:
                add_event(db, 'ready_notice_failed', user_id)
                item.notice_locked_until = utcnow() + timedelta(seconds=max(retry_after, min(300, 15 * 2**item.notice_attempts)))
