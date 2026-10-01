"""Transactional storage for the bot and worker.

PostgreSQL locks protect every quota and claim across processes. SQLite is only
a test backend; its transactions are serialized within the current event loop.
Network requests must never be performed inside these short transactions.

Rows are locked FOR NO KEY UPDATE (SQLAlchemy key_share=True without read=True):
their primary keys never change. This still serializes competing mutations but
allows foreign-key KEY SHARE locks when another transaction records usage. An
exclusive FOR UPDATE lock would deadlock user/story or story/outbox mutations
against those usage inserts despite the application not changing any key.
"""
import asyncio
import hashlib
import unicodedata
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import delete, func, or_, select, text, update

from app.domain import Analysis, Candidate, InterestSaveResult, StoryExtraction, UserError
from app.models import Feedback, Source, Story, StoryUpdate, UsageEvent, User, UserInterest, UserNews, utcnow
from app.news_repository import NewsRepository
from app.monitoring import INTENSIVE_DURATION, IntensiveSlotOccupied, completion_next, next_checkpoint

_sqlite_locks = WeakKeyDictionary()
_ADMISSION_LOCK = 419281701
_LLM_BUDGET_LOCK = 419281702
_LEASE = timedelta(minutes=15)
_DELIVERY_LEASE = timedelta(minutes=5)


def _day_start():
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


class Repository(NewsRepository):
    def __init__(self, settings, session_factory=None):
        if session_factory is None:
            from app.db import Session
            session_factory = Session
        self.settings = settings
        self.session_factory = session_factory
        self._delivery_tokens = {}

    @asynccontextmanager
    async def _transaction(self):
        async with self.session_factory() as session:
            session.sync_session.expire_on_commit = False
            sqlite = session.bind.dialect.name == "sqlite"
            lock = None
            if sqlite:
                loop = asyncio.get_running_loop()
                lock = _sqlite_locks.setdefault(loop, asyncio.Lock())
                await lock.acquire()
            try:
                async with session.begin():
                    yield session
                    await session.flush()
            finally:
                if lock is not None:
                    lock.release()

    @staticmethod
    async def _advisory(session, key):
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    @staticmethod
    async def _owned(session, user_id, story_id):
        return await session.scalar(select(Story).where(
            Story.id == story_id, Story.user_id == user_id,
            Story.status != "deleted").with_for_update(key_share=True))

    @staticmethod
    def _event(session, operation, user_id=None, story_id=None, **kwargs):
        session.add(UsageEvent(operation=operation, user_id=user_id, story_id=story_id, **kwargs))

    @staticmethod
    def _daily(story):
        story.monitoring_mode = "daily"
        story.intensive_started_at = story.intensive_until = None

    async def _expire_intensive(self, session, user_id=None):
        query = select(Story).where(Story.monitoring_mode == "intensive",
            Story.intensive_until <= utcnow(), Story.status.in_(("active", "paused")))
        if user_id is not None:
            query = query.where(Story.user_id == user_id)
        for story in (await session.scalars(query.order_by(Story.id).with_for_update(key_share=True))).all():
            self._daily(story)
            self._event(session, "intensive_expired", story.user_id, story.id)
        await session.flush()

    async def set_monitoring_mode(self, user_id, story_id, mode, replace_story_id=None):
        """Atomic assignment/transfer. Expected previous owner prevents stale confirmations."""
        if mode not in ("daily", "intensive"):
            raise ValueError("Unsupported monitoring mode")
        async with self._transaction() as session:
            user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            if user is None:
                raise UserError("Сначала откройте доступ через /start.")
            # Consistent ID order for operations touching multiple stories.
            stories = list((await session.scalars(select(Story).where(Story.user_id == user_id,
                Story.status != "deleted").order_by(Story.id).with_for_update(key_share=True))).all())
            now = utcnow()
            for item in stories:
                if item.monitoring_mode == "intensive" and item.intensive_until <= now:
                    self._daily(item)
                    self._event(session, "intensive_expired", user_id, item.id)
            story = next((item for item in stories if item.id == story_id), None)
            if story is None:
                raise UserError("Наблюдение больше недоступно. Откройте /watching.")
            if mode == "daily":
                if story.monitoring_mode == "intensive":
                    self._daily(story)
                    story.next_check_at = now + timedelta(hours=story.check_frequency_hours) if story.status == "active" else None
                    self._event(session, "intensive_disabled", user_id, story.id)
                return story
            if story.monitoring_mode == "intensive":
                return story  # Repeated taps do not restart the window.
            current = next((item for item in stories if item.monitoring_mode == "intensive"), None)
            if current is not None and current.id != replace_story_id:
                raise IntensiveSlotOccupied(current.id, current.title)
            if current is not None:
                self._daily(current)
                current.next_check_at = now + timedelta(hours=current.check_frequency_hours) if current.status == "active" else None
                self._event(session, "intensive_transferred_from", user_id, current.id)
            # Release the partial unique index before assigning the new topic.
            await session.flush()
            if story.status == "draft":
                story.status = "active"
                self._event(session, "story_created", user_id, story.id)
            await session.execute(update(UserNews).where(UserNews.story_id == story.id).values(notice_suppressed=True))
            story.monitoring_mode = "intensive"
            story.intensive_started_at = now
            story.intensive_until = now + INTENSIVE_DURATION
            story.next_check_at = next_checkpoint(now, now) if story.status == "active" else None
            story.updated_at = now
            self._event(session, "intensive_enabled", user_id, story.id)
            return story

    async def get_user(self, user_id):
        async with self._transaction() as session:
            return await session.get(User, user_id)

    async def save_interest(self, user_id, story_id):
        async with self._transaction() as session:
            # Serialize save/remove and duplicate taps across bot/API processes.
            user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            if user is None:
                raise UserError("Сначала откройте доступ через /start.")
            story = await session.scalar(select(Story).where(Story.id == story_id,
                Story.user_id == user_id).with_for_update(key_share=True))
            if story is None:
                raise UserError("Тема недоступна. Пришлите новость или текст заново.")
            normalized = " ".join(unicodedata.normalize("NFKC", story.original_input).casefold().split())
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
            existing = await session.scalar(select(UserInterest).where(
                UserInterest.user_id == user_id, or_(UserInterest.source_story_id == story_id,
                    UserInterest.input_fingerprint == fingerprint)))
            if existing is not None:
                if story.status == "draft":
                    await self._redact(session, story)
                return InterestSaveResult(existing, False, story.status)
            if story.status == "deleted":
                raise UserError("Тема больше недоступна. Пришлите новость или текст заново.")
            interest = UserInterest(user_id=user_id, source_story_id=story.id, title=story.title,
                summary=story.summary, entities=list(story.entities), keywords=list(story.keywords),
                source_url=story.original_url, input_fingerprint=fingerprint)
            session.add(interest)
            await session.flush()
            if story.status == "draft":
                # The explicit interest replaces this unconfirmed draft, so it
                # does not occupy the story quota or enter the worker schedule.
                await self._redact(session, story)
            self._event(session, "interest_saved", user_id=user_id)
            return InterestSaveResult(interest, True, story.status)

    async def list_interests(self, user_id, before_id=0, limit=9):
        async with self._transaction() as session:
            query = select(UserInterest).where(UserInterest.user_id == user_id)
            if before_id:
                query = query.where(UserInterest.id < before_id)
            return list((await session.scalars(query.order_by(UserInterest.id.desc()).limit(limit))).all())

    async def get_interest(self, user_id, interest_id):
        async with self._transaction() as session:
            return await session.scalar(select(UserInterest).where(
                UserInterest.id == interest_id, UserInterest.user_id == user_id))

    async def remove_interest(self, user_id, interest_id):
        async with self._transaction() as session:
            await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            interest = await session.scalar(select(UserInterest).where(
                UserInterest.id == interest_id, UserInterest.user_id == user_id).with_for_update(key_share=True))
            if interest is None:
                return False
            await session.delete(interest)
            self._event(session, "interest_removed", user_id=user_id)
            return True

    async def _admit(self, user_id, username, first_name, is_admin, claim):
        async with self._transaction() as session:
            await self._advisory(session, _ADMISSION_LOCK)
            if claim and await session.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))):
                return None
            user = await session.get(User, user_id)
            if user is None:
                testers = await session.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(False)))
                if not is_admin and testers >= self.settings.max_testers:
                    return None
                user = User(telegram_id=user_id, is_admin=is_admin)
                session.add(user)
                self._event(session, "user_started_bot", user_id)
            elif is_admin:
                user.is_admin = True
            user.username = username[:128] if username else None
            user.first_name = first_name[:256] if first_name else None
            user.last_seen_at = utcnow()
            # Flush the parent first even without ORM relationships.
            await session.flush([user])
            return user

    async def admit_user(self, user_id, username=None, first_name=None, is_admin=False):
        return await self._admit(user_id, username, first_name, is_admin, False)

    async def claim_admin(self, user_id, username=None, first_name=None):
        return await self._admit(user_id, username, first_name, True, True)

    async def _redact(self, session, story):
        await session.execute(update(UserNews).where(UserNews.story_id == story.id).values(notice_suppressed=True))
        await session.execute(delete(Feedback).where(Feedback.story_id == story.id))
        await session.execute(delete(Source).where(Source.story_id == story.id))
        await session.execute(delete(StoryUpdate).where(StoryUpdate.story_id == story.id))
        await session.execute(update(UsageEvent).where(UsageEvent.story_id == story.id).values(
            user_id=None, story_id=None, detail=None))
        story.title = "Удалено"
        story.original_input = story.summary = story.current_state = ""
        story.original_url = None
        story.entities = []
        story.keywords = []
        story.search_queries = []
        story.watch_goals = []
        story.status = "deleted"
        self._daily(story)
        story.lock_until = story.lock_token = story.next_check_at = None
        story.updated_at = utcnow()

    async def create_draft(self, user_id, original_input, original_url, extraction: StoryExtraction):
        async with self._transaction() as session:
            user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            if user is None:
                raise UserError("Сначала откройте доступ через /start.")
            expired = (await session.scalars(select(Story).where(
                Story.user_id == user_id, Story.status == "draft",
                Story.created_at < utcnow() - timedelta(hours=24)).with_for_update(key_share=True))).all()
            for old in expired:
                await self._redact(session, old)
            await session.flush()
            count = await session.scalar(select(func.count()).select_from(Story).where(
                Story.user_id == user_id, Story.status != "deleted"))
            if count >= self.settings.max_stories_per_user:
                raise UserError(f"Лимит — {self.settings.max_stories_per_user} наблюдений. Удалите ненужное и попробуйте снова.")
            story = Story(user_id=user_id, title=extraction.title, original_input=original_input,
                original_url=original_url, summary=extraction.short_summary,
                current_state=extraction.current_state, entities=extraction.entities,
                keywords=extraction.keywords, search_queries=extraction.search_queries,
                watch_goals=extraction.watch_goals, status="draft",
                check_frequency_hours=self.settings.default_check_interval_hours)
            session.add(story)
            await session.flush([story])
            self._event(session, "story_parsed", user_id, story.id)
            return story

    async def activate_story(self, user_id, story_id):
        async with self._transaction() as session:
            story = await self._owned(session, user_id, story_id)
            if story is None or story.status not in ("draft", "active"):
                raise UserError("Черновик не найден. Пришлите тему заново.")
            if story.status == "draft":
                story.status = "active"
                story.next_check_at = utcnow()
                story.updated_at = utcnow()
                self._event(session, "story_created", user_id, story.id)
            await session.execute(update(UserNews).where(UserNews.story_id == story.id).values(notice_suppressed=True))
            return story

    async def list_stories(self, user_id):
        async with self._transaction() as session:
            await self._expire_intensive(session, user_id)
            return list((await session.scalars(select(Story).where(Story.user_id == user_id,
                Story.status.in_(("active", "paused"))).order_by(Story.created_at.desc(), Story.id.desc()))).all())

    async def get_story(self, user_id, story_id):
        async with self._transaction() as session:
            await self._expire_intensive(session, user_id)
            return await session.scalar(select(Story).where(Story.id == story_id,
                Story.user_id == user_id, Story.status != "deleted"))

    async def set_status(self, user_id, story_id, status):
        if status not in ("active", "paused", "deleted"):
            raise ValueError("Unsupported story status")
        async with self._transaction() as session:
            story = await self._owned(session, user_id, story_id)
            if story is None:
                return None
            if status == "deleted":
                old_status = story.status
                await self._redact(session, story)
                self._event(session, "story_cancelled" if old_status == "draft" else "story_deleted")
            elif story.status == "draft":
                raise UserError("Сначала подтвердите создание наблюдения.")
            elif story.status != status:
                if story.monitoring_mode == "intensive" and story.intensive_until <= utcnow():
                    self._daily(story)
                story.status = status
                story.lock_until = story.lock_token = None
                story.updated_at = utcnow()
                story.next_check_at = utcnow() if status == "active" else None
                if status == "active" and story.monitoring_mode == "intensive":
                    story.next_check_at = next_checkpoint(story.intensive_started_at, utcnow())
                self._event(session, "story_resumed" if status == "active" else "story_paused", user_id, story.id)
            return story

    async def claim_story(self, story_id, user_id=None, manual=False):
        if manual and user_id is None:
            raise ValueError("Manual checks require an owner")
        async with self._transaction() as session:
            now = utcnow()
            # Per-user lock serializes daily quota/cooldown across all their stories.
            if manual:
                user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
                if user is None:
                    return None
            query = select(Story).where(Story.id == story_id)
            if user_id is not None:
                query = query.where(Story.user_id == user_id)
            story = await session.scalar(query.with_for_update(key_share=True))
            if story is None or story.status != "active":
                return None
            if story.lock_until is not None and story.lock_until > now:
                return None
            if user_id is None and (story.next_check_at is None or story.next_check_at > now):
                return None
            if manual:
                day = _day_start()
                if user.manual_quota_day != day:
                    user.manual_quota_day = day
                    user.manual_checks_today = 0
                if user.manual_checks_today >= self.settings.max_manual_checks_per_day:
                    raise UserError("Лимит ручных проверок на сегодня исчерпан. Автоматические проверки продолжатся.")
                last = user.last_manual_check_at
                if last is not None and last + timedelta(seconds=self.settings.manual_check_cooldown_seconds) > now:
                    raise UserError("Подождите немного перед следующей ручной проверкой.")
                user.manual_checks_today += 1
                user.last_manual_check_at = now
                story.last_manual_check_at = now
                self._event(session, "manual_check_requested", user_id, story.id)
            story.lock_token = uuid4().hex
            story.lock_until = now + _LEASE
            self._event(session, "story_check_started", story.user_id, story.id)
            return story

    async def due_story_ids(self, limit=20):
        async with self._transaction() as session:
            await self._expire_intensive(session)
            now = utcnow()
            return list((await session.scalars(select(Story.id).where(
                Story.status == "active", Story.next_check_at <= now,
                or_(Story.lock_until.is_(None), Story.lock_until <= now))
                .order_by(Story.next_check_at, Story.id).limit(max(0, limit)))).all())

    async def finish_check(self, story_id, lock_token, error=False):
        async with self._transaction() as session:
            story = await session.scalar(select(Story).where(Story.id == story_id).with_for_update(key_share=True))
            if story is None or story.status != "active" or not lock_token or story.lock_token != lock_token:
                return False
            now = utcnow()
            if story.lock_until is None or story.lock_until <= now:
                return False
            story.lock_until = story.lock_token = None
            story.next_check_at = completion_next(story, now, error)
            if story.monitoring_mode == "intensive" and story.intensive_until <= now:
                self._daily(story)
                self._event(session, "intensive_expired", story.user_id, story.id)
            story.updated_at = now
            if not error:
                story.last_checked_at = now
                self._event(session, "story_check_completed", story.user_id, story.id)
            return True

    async def known_sources(self, story_id):
        async with self._transaction() as session:
            return list((await session.scalars(select(Source).where(Source.story_id == story_id)
                .order_by(Source.created_at.desc(), Source.id.desc()).limit(100))).all())

    async def known_url_set(self, story_id):
        async with self._transaction() as session:
            return set((await session.scalars(select(Source.normalized_url)
                .where(Source.story_id == story_id))).all())

    async def save_check(self, story_id, lock_token, candidates: list[Candidate], analysis: Analysis | None):
        async with self._transaction() as session:
            story = await session.scalar(select(Story).where(Story.id == story_id).with_for_update(key_share=True))
            now = utcnow()
            if (story is None or story.status != "active" or not lock_token or story.lock_token != lock_token
                or story.lock_until is None or story.lock_until <= now):
                return None
            existing = list((await session.scalars(select(Source).where(Source.story_id == story_id))).all())
            by_url = {source.normalized_url: source for source in existing}
            hashes = {source.content_hash for source in existing}
            added = False
            for candidate in candidates:
                previous = by_url.get(candidate.normalized_url)
                if previous is not None:
                    if previous.content_hash != candidate.content_hash and candidate.full_text:
                        # Update the observed version, retaining URL uniqueness.
                        added = added or candidate.content_hash not in hashes
                        previous.content_hash = candidate.content_hash
                        previous.content_excerpt = candidate.content_excerpt
                        previous.title = candidate.title
                        previous.fetched_at = now
                        previous.relevance_score = analysis.confidence if analysis and analysis.relevant else 0
                        hashes.add(candidate.content_hash)
                    continue
                duplicate = candidate.content_hash in hashes
                source = Source(story_id=story_id, url=candidate.url, normalized_url=candidate.normalized_url,
                    domain=candidate.domain, title=candidate.title, published_at=candidate.published_at,
                    content_hash=candidate.content_hash, content_excerpt=candidate.content_excerpt,
                    search_query=candidate.search_query, relevance_score=analysis.confidence if analysis and analysis.relevant else 0,
                    is_duplicate=duplicate)
                session.add(source)
                by_url[candidate.normalized_url] = source
                hashes.add(candidate.content_hash)
                added = added or not duplicate
            if not added or analysis is None or not analysis.meaningful_update or not analysis.relevant:
                return None
            evidence = [candidate for candidate in candidates if candidate.url in analysis.source_urls]
            context_only = bool(evidence) and all(candidate.published_at is None or
                candidate.published_at < story.created_at for candidate in evidence)
            result = StoryUpdate(story_id=story_id, summary=analysis.notification_summary,
                new_facts=analysis.new_facts, previous_state=story.current_state, new_state=analysis.updated_state,
                importance_score=analysis.importance_score, confidence_score=analysis.confidence,
                source_urls=analysis.source_urls, reason=analysis.reason, is_demo=False,
                update_kind="context" if context_only else "development")
            session.add(result)
            story.current_state = analysis.updated_state
            story.last_meaningful_update_at = now
            story.updated_at = now
            self._event(session, "meaningful_update_found", story.user_id, story_id)
            return result

    async def recent_updates(self, user_id, story_id, limit=5):
        async with self._transaction() as session:
            return list((await session.scalars(select(StoryUpdate).join(Story, Story.id == StoryUpdate.story_id)
                .where(Story.user_id == user_id, Story.id == story_id, Story.status != "deleted")
                .order_by(StoryUpdate.created_at.desc(), StoryUpdate.id.desc()).limit(max(0, min(limit, 20))))).all())

    async def list_notifications(self, user_id, window, before_id=0, limit=9):
        async with self._transaction() as session:
            query = select(StoryUpdate, Story).join(Story, Story.id == StoryUpdate.story_id).where(
                Story.user_id == user_id, Story.status != 'deleted',
                StoryUpdate.notified_at >= window.since, StoryUpdate.notified_at < window.until)
            if before_id:
                cursor = (await session.execute(query.where(StoryUpdate.id == before_id))).first()
                if cursor is None:
                    raise UserError('Эта страница больше недоступна. Откройте журнал заново: /journal.')
                last = cursor[0]
                query = query.where(or_(StoryUpdate.notified_at < last.notified_at,
                    (StoryUpdate.notified_at == last.notified_at) & (StoryUpdate.id < last.id)))
            rows = (await session.execute(query.order_by(StoryUpdate.notified_at.desc(), StoryUpdate.id.desc())
                                          .limit(max(1, min(limit, 9))))).all()
            return [(item, story) for item, story in rows]

    async def get_notification(self, user_id, update_id, window):
        async with self._transaction() as session:
            row = (await session.execute(select(StoryUpdate, Story).join(Story, Story.id == StoryUpdate.story_id)
                .where(StoryUpdate.id == update_id, Story.user_id == user_id, Story.status != 'deleted',
                    StoryUpdate.notified_at >= window.since, StoryUpdate.notified_at < window.until))).first()
            return (row[0], row[1]) if row else None

    async def feedback(self, user_id, update_id, kind):
        if kind not in ("useful", "not_useful"):
            return False
        async with self._transaction() as session:
            # Story lock makes feedback upsert/delete consistent and serializes repeat clicks.
            story = await session.scalar(select(Story).join(StoryUpdate, StoryUpdate.story_id == Story.id)
                .where(StoryUpdate.id == update_id, Story.user_id == user_id,
                    Story.status != "deleted").with_for_update(of=Story, key_share=True))
            if story is None:
                return False
            existing = await session.get(Feedback, (user_id, update_id))
            if existing is None:
                session.add(Feedback(user_id=user_id, update_id=update_id, story_id=story.id, feedback_type=kind))
            else:
                existing.feedback_type = kind
            self._event(session, "notification_feedback_" + kind, user_id, story.id)
            return True

    async def pending_notifications(self, limit=10):
        async with self._transaction() as session:
            now = utcnow()
            rows = (await session.execute(select(StoryUpdate, Story).join(Story, Story.id == StoryUpdate.story_id)
                .where(Story.status == "active", StoryUpdate.notified_at.is_(None), StoryUpdate.delivery_attempts < 5,
                    or_(StoryUpdate.delivery_locked_until.is_(None), StoryUpdate.delivery_locked_until <= now))
                .order_by(StoryUpdate.created_at, StoryUpdate.id).limit(max(0, limit))
                .with_for_update(skip_locked=True, of=StoryUpdate, key_share=True))).all()
            for item, _ in rows:
                item.delivery_attempts += 1
                item.delivery_locked_until = now + _DELIVERY_LEASE
                item.delivery_lock_token = uuid4().hex
                self._delivery_tokens[item.id] = item.delivery_lock_token
            return [(item, story) for item, story in rows]

    async def mark_notified(self, update_id, success: bool, delivery_token=None, telegram_message_id=None):
        token = delivery_token or self._delivery_tokens.get(update_id)
        if not token:
            return
        async with self._transaction() as session:
            item = await session.scalar(select(StoryUpdate).where(StoryUpdate.id == update_id).with_for_update(key_share=True))
            now = utcnow()
            if (item is None or item.notified_at is not None or item.delivery_lock_token != token
                or item.delivery_locked_until is None or item.delivery_locked_until <= now):
                return
            item.delivery_lock_token = None
            if success:
                item.notified_at = now
                if type(telegram_message_id) is int and telegram_message_id > 0:
                    item.telegram_message_id = telegram_message_id
                item.delivery_locked_until = None
                self._event(session, "notification_sent", story_id=item.story_id)
            else:
                # Persist backoff, including across process restarts.
                item.delivery_locked_until = now + timedelta(seconds=min(300, 15 * 2 ** item.delivery_attempts))
            self._delivery_tokens.pop(update_id, None)

    async def create_demo(self, user_id, story_id):
        async with self._transaction() as session:
            story = await self._owned(session, user_id, story_id)
            if story is None or story.status != "active":
                raise UserError("Для демонстрации нужно активное наблюдение.")
            item = StoryUpdate(story_id=story.id, summary="ДЕМО: так будет выглядеть уведомление о развитии истории.",
                new_facts=["Это тестовое сообщение, а не найденная новость."], previous_state=story.current_state,
                new_state=story.current_state, importance_score=1, confidence_score=1, source_urls=[],
                reason="Проверка доставки и кнопок обратной связи. Состояние наблюдения не изменено.", is_demo=True)
            session.add(item)
            self._event(session, "demo_update_created", user_id, story.id)
            return item

    async def record_usage(self, operation, provider="", user_id=None, story_id=None, input_tokens=0,
                           output_tokens=0, estimated_cost=0, detail=None, model=None, **kwargs):
        async with self._transaction() as session:
            # A check can finish after deletion: preserve counts without resurrecting content.
            if story_id is not None:
                story = await session.scalar(select(Story).where(Story.id == story_id).with_for_update(key_share=True))
                if story is None or story.status == "deleted":
                    story_id = user_id = detail = None
            self._event(session, operation[:80], user_id, story_id, provider=provider[:80],
                model=model[:160] if model else None, input_tokens=max(0, input_tokens),
                output_tokens=max(0, output_tokens), estimated_cost=max(0, estimated_cost), detail=detail)

    async def reserve_llm_call(self, user_id=None, story_id=None):
        async with self._transaction() as session:
            await self._advisory(session, _LLM_BUDGET_LOCK)
            count = await session.scalar(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.operation == "llm_reserved", UsageEvent.created_at >= _day_start()))
            if count >= self.settings.llm_daily_call_limit:
                return False
            if story_id is not None:
                story = await session.scalar(select(Story).where(Story.id == story_id).with_for_update(key_share=True))
                if story is None or story.status == "deleted":
                    story_id = user_id = None
            self._event(session, "llm_reserved", user_id, story_id)
            return True

    async def usage_count_today(self, operation):
        async with self._transaction() as session:
            return await session.scalar(select(func.count()).select_from(UsageEvent).where(
                UsageEvent.operation == operation, UsageEvent.created_at >= _day_start()))

    async def admin_stats(self):
        async with self._transaction() as session:
            today = _day_start()
            counts = dict((await session.execute(select(UsageEvent.operation, func.count())
                .where(UsageEvent.created_at >= today).group_by(UsageEvent.operation))).all())
            statuses = dict((await session.execute(select(Story.status, func.count()).group_by(Story.status))).all())
            feedback = dict((await session.execute(select(Feedback.feedback_type, func.count())
                .join(StoryUpdate, StoryUpdate.id == Feedback.update_id)
                .where(StoryUpdate.is_demo.is_(False)).group_by(Feedback.feedback_type))).all())
            users = await session.scalar(select(func.count()).select_from(User))
            real_updates = await session.scalar(select(func.count()).select_from(StoryUpdate).where(StoryUpdate.is_demo.is_(False)))
            cost_today = await session.scalar(select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0)).where(UsageEvent.created_at >= today))
            cost_total = await session.scalar(select(func.coalesce(func.sum(UsageEvent.estimated_cost), 0)))
            with_stories = await session.scalar(select(func.count(func.distinct(Story.user_id))).where(Story.status.in_(("active", "paused"))))
            user_rows = list((await session.scalars(select(User))).all())
            returned = sum(user.last_seen_at >= user.created_at + timedelta(days=1) for user in user_rows)
            sent = await session.scalar(select(func.count()).select_from(StoryUpdate).where(
                StoryUpdate.is_demo.is_(False), StoryUpdate.notified_at.is_not(None)))
            votes = sum(feedback.values())
            return {"users": users, "users_with_stories": with_stories, "active_stories": statuses.get("active", 0),
                "average_stories_per_user": (statuses.get("active",0)+statuses.get("paused",0))/max(users,1),
                "returned_users": returned, "notifications_sent": sent,
                "useful_percent": 100*feedback.get("useful",0)/votes if votes else 0,
                "cost_per_active_user": float(cost_total)/with_stories if with_stories else 0,
                "paused_stories": statuses.get("paused", 0), "deleted_stories": statuses.get("deleted", 0),
                "draft_stories": statuses.get("draft", 0), "checks_today": counts.get("story_check_started", 0),
                "manual_checks_today": counts.get("manual_check_requested", 0),
                "searches_today": sum(v for k, v in counts.items() if k in ("search", "search_query", "search_request")),
                "llm_calls_today": counts.get("llm_reserved", 0), "meaningful_updates": real_updates,
                "errors_today": sum(v for k, v in counts.items() if "error" in k),
                "feedback_useful": feedback.get("useful", 0), "feedback_not_useful": feedback.get("not_useful", 0),
                "estimated_cost_today": float(cost_today), "estimated_cost_total": float(cost_total),
                "operations_today": counts}

    async def recent_errors(self, limit=10):
        async with self._transaction() as session:
            return list((await session.scalars(select(UsageEvent).where(UsageEvent.operation.contains("error"))
                .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc()).limit(max(0, min(limit, 50))))).all())
