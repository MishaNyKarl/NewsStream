"""Application boundary: ownership, usage budgets and the monitoring workflow."""
import asyncio
import hmac
import json
import logging
import re
import time
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

from app.ai.client import AIClient
from app.commerce import Commerce, operation_key
from app.config import Settings
from app.content.fetcher import ContentFetcher
from app.domain import Candidate, ProviderUnavailable, UserError
from app.repository import Repository
from app.search.client import SearchClient
from app.search.dedupe import content_hash, is_near_duplicate, normalize_url
from app.search.planning import diverse_results, plan_queries, publisher, source_cutoff
from app.progress import CheckMetrics, CheckOutcome, progress_context, report

log = logging.getLogger(__name__)
credit_context: ContextVar[str | None] = ContextVar('credit_context', default=None)
usage_context: ContextVar[dict] = ContextVar('usage_context', default={})

def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

class BotService:
    def __init__(self, settings: Settings, repo=None, ai=None, search=None, fetcher=None):
        self.settings = settings
        self.repo = repo or Repository(settings)
        self.commerce = Commerce(self.repo.session_factory, settings) if isinstance(self.repo, Repository) else None
        self.ai = ai or AIClient(settings, self._usage)
        self.search = search or SearchClient(settings, self._usage)
        self.fetcher = fetcher or ContentFetcher()
        self._creating: set[int] = set()
        self._tasks: set[asyncio.Task] = set()
        self._checks = asyncio.Semaphore(2)
        self._manual_admission = asyncio.Lock()

    async def _usage(self, operation, provider='', **kwargs):
        context = usage_context.get()
        if operation == 'llm_attempt':
            if not await self.repo.reserve_llm_call(**context):
                raise UserError('Дневной лимит анализа исчерпан. Попробуйте завтра.')
            return
        await self.repo.record_usage(operation=operation, provider=provider, **context, **kwargs)

    def provider_ready(self):
        return self.settings.llm_provider != 'disabled' and bool(self.settings.llm_model) and (
            bool(self.settings.llm_api_key) or self.settings.llm_provider == 'ollama')

    async def authorize(self, user_id, username=None, first_name=None, start_arg=''):
        admin_secret = self.settings.admin_claim_token
        if admin_secret and start_arg.startswith('admin_') and hmac.compare_digest(start_arg[6:], admin_secret):
            claimed = await self.repo.claim_admin(user_id, username=username, first_name=first_name)
            if claimed:
                return claimed
        existing = await self.repo.get_user(user_id)
        if existing:
            return await self.repo.admit_user(user_id, username=username, first_name=first_name,
                is_admin=existing.is_admin or user_id in self.settings.admin_ids)
        admin = user_id in self.settings.admin_ids
        invited = bool(self.settings.invite_code) and start_arg.startswith('invite_') and hmac.compare_digest(start_arg[7:], self.settings.invite_code)
        if not (admin or user_id in self.settings.allowed_ids or invited):
            return None
        return await self.repo.admit_user(user_id, username=username, first_name=first_name, is_admin=admin)

    async def track_interaction(self, user_id, category, event_key):
        try:
            await self.repo.record_product_event('interaction_' + category, user_id, event_key)
        except Exception as exc:
            # Observability must not prevent the original user action; no raw payload in logs.
            log.warning('analytics_interaction_failed type=%s', type(exc).__name__)

    async def track_membership(self, user_id, blocked, event_key):
        try:
            await self.repo.record_product_event('bot_blocked' if blocked else 'bot_unblocked', user_id, event_key)
        except Exception as exc:
            log.warning('analytics_membership_failed type=%s', type(exc).__name__)

    async def is_admin(self, user_id):
        user = await self.repo.get_user(user_id)
        inherited = bool(user and (user.is_admin or user_id in self.settings.admin_ids))
        return await self.commerce.role(user_id, inherited) if self.commerce else inherited

    async def _require_user(self, user_id):
        if not await self.repo.get_user(user_id):
            raise UserError('Доступ только по приглашению. Откройте вашу пригласительную ссылку.')

    async def save_user_news(self, user_id, text, *, source_url=None, use_text=False, input_message_id=None):
        return await self.repo.save_user_news(user_id, text, source_url, use_text, input_message_id)

    async def list_user_news(self, user_id, before_id=0):
        return await self.repo.list_user_news(user_id, before_id)

    async def get_user_news(self, user_id, news_id):
        return await self.repo.get_user_news(user_id, news_id)

    async def user_news_story(self, user_id, news_id):
        return await self.repo.user_news_story(user_id, news_id)

    async def user_news_interest(self, user_id, news_id):
        return await self.repo.user_news_interest(user_id, news_id)

    async def defer_user_news(self, user_id, news_id):
        return await self.repo.defer_user_news(user_id, news_id)

    async def delete_user_news(self, user_id, news_id):
        return await self.repo.delete_user_news(user_id, news_id)

    async def process_user_news(self, user_id, news_id, progress=None):
        item = await self.repo.claim_user_news(user_id, news_id)
        if item.status == 'ready':
            return item
        try:
            return await self.prepare_story(user_id, item.original_text, progress, source_url=item.source_url,
                use_text=item.use_text, news_id=item.id, news_token=item.processing_token)
        except (Exception, asyncio.CancelledError) as exc:
            error = str(exc) if isinstance(exc, UserError) else 'Обработка прервана. Можно повторить из списка новостей.'
            try:
                await asyncio.shield(self.repo.finish_user_news(user_id, item.id, item.processing_token, error=error))
            except UserError:
                pass  # Deleted/reclaimed work must never resurrect its record.
            raise

    async def prepare_story(self, user_id, text, progress=None, *, source_url=None, use_text=False,
                            news_id=None, news_token=None):
        token = progress_context.set(progress)
        charge = None
        credit_token = credit_context.set(None)
        success = False
        try:
            async with asyncio.timeout(300):
                await self._require_user(user_id)
                if self.commerce:
                    charge = await self.commerce.reserve(user_id, 'news', operation_key())
                    credit_context.set(charge)
                result = await self._prepare_story(user_id, text, source_url=source_url, use_text=use_text,
                                                   news_id=news_id, news_token=news_token)
                success = True
                return result
        except TimeoutError:
            raise UserError('Подготовка заняла слишком много времени. Пришлите ссылку или описание ещё раз.') from None
        finally:
            progress_context.reset(token)
            credit_context.reset(credit_token)
            if charge:
                await asyncio.shield(self.commerce.settle(user_id, charge, success))

    async def _prepare_story(self, user_id, text, *, source_url=None, use_text=False, news_id=None, news_token=None):
        await self._require_user(user_id)
        if not self.provider_ready():
            raise ProviderUnavailable('Анализ пока не настроен. Администратору нужно подключить ключ нейросети.')
        text = text.strip()
        if not 10 <= len(text) <= 10000:
            raise UserError('Пришлите ссылку или описание длиной от 10 до 10 000 символов.')
        if user_id in self._creating:
            raise UserError('Ещё разбираю предыдущее сообщение. Подождите немного.')
        self._creating.add(user_id)
        ctx = usage_context.set({'user_id': user_id, 'request_id': credit_context.get() or uuid4().hex})
        try:
            await self.repo.record_usage('story_input_received', user_id=user_id)
            # Only public forwarded-post references are accepted as provenance.
            # They are never fetched when the actual post text is provided.
            url = source_url if source_url and re.fullmatch(r'https://t\.me/[A-Za-z0-9_]{1,64}/[1-9][0-9]*', source_url) else None
            urls = re.findall(r'https?://[^\s<>]+', text)
            if use_text and len(re.sub(r'https?://[^\s<>]+', '', text).strip()) < 10:
                raise UserError('В посте почти нет текста новости. Перешлите пост с описанием события или отправьте ссылку на статью отдельным сообщением.')
            if urls and not use_text:
                url = urls[0].rstrip('.,;)')
                # Fetch performs DNS-pinned URL and redirect validation.
                try:
                    await report('reading_input')
                    page = await self.fetcher.fetch(url)
                    if len(page.text.strip()) < 80:
                        raise ValueError('not enough readable source content')
                    text_for_ai = f'USER INPUT:\n{text}\nUNTRUSTED PAGE TITLE:\n{page.title}\nUNTRUSTED PAGE CONTENT:\n{page.text[:14000]}'
                except Exception:
                    raise UserError('Не удалось безопасно прочитать эту страницу. Пришлите текст новости или кратко опишите сюжет.') from None
            else:
                text_for_ai = text
            await report('extracting')
            extraction = await self.ai.extract(text_for_ai, url=url)
            extraction.search_queries = extraction.search_queries[:self.settings.max_search_queries_per_story]
            await report('saving_draft')
            if news_id is not None:
                return await self.repo.finish_user_news(user_id, news_id, news_token, extraction, url)
            story = await self.repo.create_draft(user_id, text, url, extraction)
            await self.repo.record_usage('story_draft', user_id=user_id, story_id=story.id)
            return story
        except UserError:
            raise
        except Exception as exc:
            await self._error('extract', exc, user_id=user_id)
            raise UserError('Сейчас не удалось разобрать сюжет. Попробуйте ещё раз через минуту.') from None
        finally:
            usage_context.reset(ctx)
            self._creating.discard(user_id)

    async def confirm_story(self, user_id, story_id):
        return await self.repo.activate_story(user_id, story_id)

    async def save_interest(self, user_id, story_id):
        return await self.repo.save_interest(user_id, story_id)

    async def list_notifications(self, user_id, window, before_id=0):
        return await self.repo.list_notifications(user_id, window, before_id=before_id)

    async def get_notification(self, user_id, update_id, window):
        return await self.repo.get_notification(user_id, update_id, window)

    async def list_interests(self, user_id, before_id=0):
        return await self.repo.list_interests(user_id, before_id=before_id)

    async def get_interest(self, user_id, interest_id):
        return await self.repo.get_interest(user_id, interest_id)

    async def remove_interest(self, user_id, interest_id):
        return await self.repo.remove_interest(user_id, interest_id)

    async def set_monitoring_mode(self, user_id, story_id, mode, replace_story_id=None):
        return await self.repo.set_monitoring_mode(user_id, story_id, mode, replace_story_id)

    async def cancel_draft(self, user_id, story_id):
        story = await self.repo.get_story(user_id, story_id)
        if story and story.status == 'draft':
            await self.repo.set_status(user_id, story_id, 'deleted')

    async def list_stories(self, user_id):
        return await self.repo.list_stories(user_id)

    async def get_story(self, user_id, story_id):
        return await self.repo.get_story(user_id, story_id)

    async def set_status(self, user_id, story_id, status):
        if status not in {'active', 'paused', 'deleted'}:
            raise UserError('Неизвестное действие.')
        return await self.repo.set_status(user_id, story_id, status)

    async def request_check(self, user_id, story_id, progress=None, on_complete=None):
        await self._require_user(user_id)
        if not self.provider_ready():
            raise ProviderUnavailable('Анализ пока не настроен. Нужен ключ нейросети.')
        async with self._manual_admission:
            if len(self._tasks) >= 2:
                raise UserError('Сейчас выполняются другие проверки. Попробуйте через пару минут — ваш лимит не потрачен.')
            story = await self.repo.claim_story(story_id, user_id=user_id, manual=True)
            if not story:
                raise UserError('Наблюдение уже проверяется, приостановлено или недоступно.')
            task = asyncio.create_task(self._manual_check(story, progress, on_complete), name=f'check-{story.id}')
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return 'Проверка запущена. Покажу этапы работы и результат в этом сообщении.'

    async def _manual_check(self, story, progress, on_complete):
        outcome = CheckOutcome('error', message='Не удалось завершить проверку. Попробуйте позже.')
        try:
            outcome = await self.check_story(story, progress=progress) if progress else await self.check_story(story)
        except asyncio.CancelledError:
            outcome = CheckOutcome('cancelled', message='Проверка прервана перезапуском бота. Автоматическое наблюдение продолжится.')
            raise
        except Exception as exc:
            await self._error('manual_check', exc, story_id=story.id, user_id=story.user_id)
        finally:
            if on_complete is not None:
                try:
                    async with asyncio.timeout(15):
                        await on_complete(outcome)
                except Exception as exc:
                    await self._error('check_status', exc, story_id=story.id, user_id=story.user_id)

    async def _candidates(self, story, metrics=None):
        metrics = metrics if metrics is not None else CheckMetrics()
        known = await self.repo.known_sources(story.id)
        known_urls = await self.repo.known_url_set(story.id)
        known_by_url = {source.normalized_url: source for source in known}
        hashes = {source.content_hash for source in known}
        excerpts = [source.content_excerpt for source in known if source.content_excerpt]
        results = []
        successful_queries = 0
        queries = plan_queries(story, self.settings)
        for number, query in enumerate(queries, 1):
            await report('searching', current=number, total=len(queries), results=len(results))
            try:
                batch = await self.search.search(query)
                results.extend(batch)
                providers = getattr(batch, 'providers', (self.settings.search_provider,))
                metrics.queries += len(providers)
                metrics.providers.update(providers)
                metrics.queries_failed += len(getattr(batch, 'failed_providers', ()))
                successful_queries += 1
            except Exception as exc:
                attempted = 2 if self.settings.search_provider == 'hybrid_news' else 1
                metrics.queries_failed += attempted
                metrics.queries += attempted
                await self._error('search', exc, story_id=story.id, user_id=story.user_id)
        if not successful_queries:
            raise ProviderUnavailable('Поиск временно недоступен.')
        metrics.results = len(results)
        now = datetime.now(timezone.utc)
        cutoff = source_cutoff(story, self.settings, now)
        unique = {}
        for result in results:
            try:
                normalized = normalize_url(result.url)
            except (ValueError, TypeError):
                continue
            if len(normalized.encode('utf-8')) > 2000:
                continue
            if result.published_at and not cutoff <= utc(result.published_at) <= now + timedelta(minutes=30):
                metrics.outside_window += 1
                continue
            if normalized in unique:
                metrics.duplicates += 1
                previous = unique[normalized]
                if result.published_at and (not previous.published_at or result.published_at > previous.published_at):
                    unique[normalized] = result
            else:
                unique[normalized] = result
        pool = []
        rechecks = 0
        reads = 0
        seen_titles = set()
        for result in diverse_results(list(unique.values()), utc(story.created_at)):
            if reads >= max(1, min(12, self.settings.max_source_reads_per_check)):
                break
            normalized = normalize_url(result.url)
            previous = known_by_url.get(normalized)
            if normalized in known_urls:
                eligible = (previous is not None and previous.fetched_at <= now - timedelta(
                    minutes=max(5, self.settings.source_recheck_minutes)))
                if not eligible or rechecks >= max(0, min(3, self.settings.max_source_rechecks)):
                    metrics.duplicates += 1
                    continue
                rechecks += 1
            title_key = (publisher(result), content_hash(result.title.rsplit(' - ', 1)[0]))
            if title_key in seen_titles:
                metrics.duplicates += 1
                continue
            seen_titles.add(title_key)
            body = result.snippet
            title = result.title
            reads += 1
            full_text = False
            await report('reading_sources', current=reads, results=len(results))
            try:
                page = await self.fetcher.fetch(result.url)
                # Google News RSS links often lead to a JS-only shell. In that case
                # keep the attributed search headline, never unrelated navigation.
                if urlsplit(page.url).hostname not in {'news.google.com', 'consent.google.com'} and len(page.text) >= 120:
                    body = page.text[:7000]
                    title = page.title or title
                    full_text = True
            except Exception:
                pass
            excerpt = (title + '\n' + (body or ''))[:7500]
            digest = content_hash(excerpt)
            # A changed article at the same URL may contain just one important
            # new sentence. Let the fact comparison decide, rather than fuzzy
            # deduplication erasing it. Never replace a full article with a
            # transient RSS fragment during a revisit.
            if (previous is not None and not full_text) or digest in hashes or (
                    previous is None and is_near_duplicate(excerpt, excerpts)):
                metrics.duplicates += 1
                await self.repo.record_usage('source_duplicate', story_id=story.id, user_id=story.user_id)
                continue
            hashes.add(digest)
            excerpts.append(excerpt)
            pool.append(Candidate(url=result.url, normalized_url=normalized,
                domain=publisher(result), title=title[:300],
                content_excerpt=excerpt, content_hash=digest, search_query=result.query,
                published_at=result.published_at, full_text=full_text))
        # Preserve publisher ordering within each group, and prefer actual
        # article bodies over headlines when the analysis budget is full.
        pool.sort(key=lambda candidate: not candidate.full_text)
        candidates = pool[:max(1, min(6, self.settings.max_sources_per_check))]
        metrics.full_texts = sum(candidate.full_text for candidate in candidates)
        metrics.snippets = len(candidates) - metrics.full_texts
        metrics.before_subscription = sum(candidate.published_at is not None and
            utc(candidate.published_at) < utc(story.created_at) for candidate in candidates)
        await self.repo.record_usage('search_summary', story_id=story.id, user_id=story.user_id,
                                     detail=json.dumps(metrics.summary(), ensure_ascii=False))
        for _ in candidates:
            await self.repo.record_usage('source_new', story_id=story.id, user_id=story.user_id)
        return candidates

    async def check_story(self, story, progress=None):
        started = time.monotonic()
        ctx = usage_context.set({'user_id': story.user_id, 'story_id': story.id, 'request_id': story.lock_token or uuid4().hex})
        progress_token = progress_context.set(progress)
        metrics = CheckMetrics()
        candidates = []
        outcome = CheckOutcome('error', message='Не удалось завершить проверку. Попробуйте позже.')
        failed = False
        charge = None
        try:
            if self.commerce:
                charge = await self.commerce.reserve(story.user_id, 'check', story.lock_token)
            await report('queued')
            async with self._checks:
                async with asyncio.timeout(600):
                    candidates = await self._candidates(story, metrics=metrics)
                    analysis = None
                    if candidates:
                        await report('analyzing', sources=len(candidates))
                        context = {key: getattr(story, key) for key in ['title','current_state','watch_goals','entities','keywords']}
                        context['monitoring_started_at'] = utc(story.created_at).isoformat()
                        context['now'] = datetime.now(timezone.utc).isoformat()
                        history = await self.repo.recent_updates(story.user_id, story.id)
                        context['known_facts'] = [fact for update in history if not update.is_demo for fact in update.new_facts]
                        analysis = await self.ai.analyze(context, candidates)
                        allowed = {candidate.url for candidate in candidates}
                        analysis.source_urls = [url for url in analysis.source_urls if url in allowed]
                        evidence = [candidate for candidate in candidates if candidate.url in analysis.source_urls]
                        if evidence and not any(candidate.full_text for candidate in evidence):
                            analysis.confidence = min(analysis.confidence, .70)
                        analysis.meaningful_update = bool(analysis.meaningful_update and analysis.relevant
                            and analysis.novelty_score >= .65 and analysis.importance_score >= .55
                            and analysis.confidence >= .75 and analysis.new_facts and analysis.source_urls
                            and analysis.notification_summary and analysis.updated_state)
                    await report('saving_result', sources=len(candidates))
                    uncertain = bool(candidates and (not any(c.full_text for c in candidates)
                        or (analysis and analysis.relevant and analysis.confidence < .75)))
                    # An inconclusive attempt must not permanently mark article
                    # URLs as processed and prevent a future, successful retry.
                    update = await self.repo.save_check(story.id, story.lock_token, [] if uncertain else candidates, analysis)
                    status = ('changed' if update else 'unverified' if uncertain
                              else 'unchanged' if candidates else 'no_sources')
                    outcome = CheckOutcome(status,
                                           sources=len(candidates), partial_search=bool(metrics.queries_failed),
                                           update_kind=getattr(update, 'update_kind', 'development'),
                                           search_summary=metrics.summary())
                    await self.repo.record_usage('check', user_id=story.user_id, story_id=story.id,
                        detail=f'sources={len(candidates)}; update={bool(update)}; seconds={time.monotonic()-started:.1f}')
        except asyncio.CancelledError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            reason = str(exc) if isinstance(exc, UserError) else (
                'Проверка заняла слишком много времени.' if isinstance(exc, TimeoutError)
                else 'Не удалось завершить проверку из-за временного сбоя.')
            outcome = CheckOutcome('error', sources=len(candidates), message=reason,
                                   partial_search=bool(metrics.queries_failed), search_summary=metrics.summary())
            await self._error('check', exc, story_id=story.id, user_id=story.user_id)
        finally:
            try:
                saved = await self.repo.finish_check(story.id, story.lock_token, error=failed)
                if saved is False:
                    outcome = CheckOutcome('cancelled', message='Эта проверка больше не актуальна: наблюдение было изменено или остановлено. Текущее состояние — в карточке темы.')
            except Exception as exc:
                outcome = CheckOutcome('error', message='Не удалось сохранить завершение проверки. Посмотрите текущее состояние темы.')
                await self._error('finish_check', exc, story_id=story.id, user_id=story.user_id)
            finally:
                usage_context.reset(ctx)
                progress_context.reset(progress_token)
                if charge:
                    await asyncio.shield(self.commerce.settle(story.user_id, charge,
                        not failed and outcome.status not in {'error', 'cancelled'}))
        return outcome

    async def account_text(self, user_id):
        import html
        value = await self.commerce.snapshot(user_id)
        account, p = value['account'], value['limits']
        return (f"<b>Тариф: {html.escape(p['name'])}</b>\n"
                f"Баланс: {account.balance if account else 0} кредитов\n"
                f"Наблюдений: {p['stories']}; срочных одновременно: {p['intensive_slots']}\n"
                f"Ручных проверок в сутки UTC: {p['manual_daily']}\n"
                f"Обсуждение: {'доступно' if p['discussion'] else 'недоступно'}\n"
                f"Разбор новости: {p['news_credits']} кр.; проверка: {p['check_credits']} кр.; "
                f"ответ в обсуждении: {p['discussion_credits']} кр.\n\n"
                "Проверки по расписанию тоже расходуют кредиты. Проверка без новых фактов оплачивается, "
                "если завершилась; при ошибке кредиты возвращаются. "
                "Пополнение и смена тарифа — через администратора. Автоматическое продление пока не подключено.")

    async def discuss(self, user_id, story_id, question):
        story = await self.repo.get_story(user_id, story_id)
        if not story:
            raise UserError('Наблюдение недоступно.')
        if not 3 <= len(question.strip()) <= 2000:
            raise UserError('Вопрос должен содержать от 3 до 2000 символов.')
        if not self.provider_ready():
            raise UserError('Обсуждение временно недоступно.')
        key = operation_key()
        charge = await self.commerce.reserve(user_id, 'discussion', key)
        context = usage_context.set({'user_id': user_id, 'story_id': story_id, 'request_id': key})
        success = False
        try:
            async with asyncio.timeout(120):
                updates = await self.repo.recent_updates(user_id, story_id)
                answer = await self.ai.discuss({'title': story.title, 'current_state': story.current_state,
                    'updates': [u.summary for u in updates[:5]], 'question': question.strip()})
                success = True
                return answer
        finally:
            usage_context.reset(context)
            await asyncio.shield(self.commerce.settle(user_id, charge, success))

    async def _error(self, operation, exc, **context):
        # Exception strings may contain a request URL, headers or user content.
        detail = f'{operation}: {type(exc).__name__}'
        log.warning('%s context=%s', detail, context)
        try:
            await self.repo.record_usage('error', detail=detail, **context)
        except Exception:
            log.error('Could not persist error counter')

    async def recent_updates(self, user_id, story_id):
        return await self.repo.recent_updates(user_id, story_id)

    async def give_feedback(self, user_id, update_id, kind):
        if kind not in {'useful', 'not_useful'}:
            raise UserError('Неизвестная оценка.')
        return await self.repo.feedback(user_id, update_id, kind)

    async def admin_summary(self, user_id):
        if not await self.is_admin(user_id):
            raise UserError('Эта команда доступна только администратору.')
        stats = await self.repo.admin_stats()
        labels = {'users':'Пользователи','active_stories':'Активные наблюдения',
            'users_with_stories':'Создали наблюдения','paused_stories':'На паузе',
            'deleted_stories':'Удалено','draft_stories':'Ожидают подтверждения',
            'manual_checks_today':'Ручных проверок сегодня',
            'checks_today':'Проверок сегодня','searches_today':'Поисков сегодня',
            'llm_calls_today':'Обращений к модели сегодня','meaningful_updates':'Существенных обновлений',
            'errors':'Ошибок','errors_today':'Ошибок сегодня','feedback_useful':'Полезно',
            'feedback_not_useful':'Неважно','estimated_cost_today':'Оценка расходов сегодня, $',
            'estimated_cost_total':'Оценка расходов за всё время, $'}
        labels.update({'average_stories_per_user':'Наблюдений на пользователя',
            'returned_users':'Вернулись спустя сутки','notifications_sent':'Отправлено уведомлений',
            'useful_percent':'Полезных оценок, %','cost_per_active_user':'Расходы на участника с наблюдениями, $'})
        lines = ['📊 Статистика закрытого теста']
        for key, value in stats.items():
            if key == 'operations_today':
                continue
            if isinstance(value, float):
                value = f'{value:.4f}'
            lines.append(f'{labels.get(key, key)}: {value}')
        lines.append(f'Лимит анализа: {self.settings.llm_daily_call_limit} обращений/сутки (UTC).')
        provider_label = {'google_news': 'Google News RSS', 'bing_news': 'Bing News RSS',
                          'hybrid_news': 'Bing News + Google News RSS'}.get(self.settings.search_provider, self.settings.search_provider)
        lines.append(f'Поиск: {provider_label}.')
        return '\n'.join(lines)

    async def admin_errors(self, user_id):
        if not await self.is_admin(user_id):
            raise UserError('Эта команда доступна только администратору.')
        errors = await self.repo.recent_errors()
        return '\n'.join(['Последние ошибки (UTC):'] + [f'{e.created_at:%d.%m %H:%M} · {e.detail}' for e in errors]) if errors else 'Ошибок пока нет.'

    async def demo_update(self, user_id, story_id=None):
        if not await self.is_admin(user_id):
            raise UserError('Демо доступно только администратору.')
        if not self.settings.demo_mode:
            raise UserError('Демо выключено. Установите DEMO_MODE=true в настройках сервера.')
        if story_id is None:
            stories = [s for s in await self.repo.list_stories(user_id) if s.status == 'active']
            if not stories:
                raise UserError('Сначала создайте активное наблюдение.')
            story_id = stories[0].id
        await self.repo.create_demo(user_id, story_id)
        return 'Тестовое уведомление поставлено в очередь. Оно явно помечено «ДЕМО» и не меняет состояние сюжета.'

    async def close(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.ai.close()
        await self.search.close()
        await self.fetcher.close()
