import asyncio
import json
import math
from datetime import datetime, timezone

import aiohttp
from pydantic import ValidationError

from app.ai.prompts import ANALYZE, EXTRACT, VERIFY
from app.domain import Analysis, Candidate, ProviderUnavailable, ReviewedAnalysis, StoryExtraction
from app.progress import report

AI_ERROR = 'Сервис анализа временно недоступен. Попробуйте позже; наблюдения сохранены.'


class _Retryable(Exception):
    pass


def evidence_passages(sources):
    """Give the model immutable excerpt IDs instead of asking it to retype quotes."""
    prepared, index = [], {}
    for number, source in enumerate(sources):
        text = ' '.join(source['content'].split())
        passages = []
        while text:
            end = len(text) if len(text) <= 800 else max(text.rfind('. ', 200, 800) + 1, text.rfind(' ', 200, 800))
            if end < 200 and len(text) > 800:
                end = 800
            excerpt, text = text[:end].strip(), text[end:].strip()
            identifier = f's{number}p{len(passages)}'
            passages.append({'id': identifier, 'text': excerpt})
            index[identifier] = {'url': source['url'], 'text': excerpt}
        prepared.append({key: value for key, value in source.items() if key != 'content'} | {'passages': passages})
    return prepared, index


def _number(value) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


class AIClient:
    def __init__(self, settings, usage_callback=None):
        self.settings = settings
        self.usage_callback = usage_callback
        self._session = None

    def _configuration(self):
        provider = self.settings.llm_provider
        defaults = {
            'openrouter': ('https://openrouter.ai/api/v1', 'qwen/qwen3-30b-a3b-instruct-2507'),
            'openai': ('https://api.openai.com/v1', 'gpt-4.1-mini'),
            'gemini': ('https://generativelanguage.googleapis.com/v1beta/openai', 'gemini-2.5-flash-lite'),
            'ollama': ('http://localhost:11434/v1', 'qwen2.5:7b'),
        }
        if provider not in defaults or (provider != 'ollama' and not self.settings.llm_api_key):
            raise ProviderUnavailable('Анализ ещё не настроен. Администратору нужно добавить API-ключ модели.')
        base, model = defaults[provider]
        return (self.settings.llm_base_url or base).rstrip('/') + '/chat/completions', self.settings.llm_model or model

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=min(120, max(5, self.settings.llm_timeout_seconds))),
                trust_env=False,
            )
        return self._session

    async def _request(self, messages: list[dict], schema, purpose: str, attempt: int) -> str:
        endpoint, model = self._configuration()
        provider = self.settings.llm_provider
        # Budget reservations are outside the try: a rejected reservation must
        # never trigger network traffic or be recorded as an API call.
        if self.usage_callback:
            await self.usage_callback(operation='llm_attempt', provider=provider, model=model,
                                      detail=f'{purpose}:{attempt + 1}')
        input_tokens = output_tokens = 0
        cost = 0.0
        cost_source, currency, actual_cost = 'unknown', None, None
        status_label = 'transport_error'
        headers = {'Authorization': f'Bearer {self.settings.llm_api_key or "ollama"}',
                   'Content-Type': 'application/json'}
        if provider == 'openrouter':
            headers['X-OpenRouter-Title'] = 'News Watch MVP'
        request = {'model': model, 'messages': messages, 'temperature': 0.1, 'max_tokens': 3000,
                   'stream': False, 'response_format': {
                       'type': 'json_schema', 'json_schema': {
                           'name': schema.__name__, 'strict': True, 'schema': schema.model_json_schema(),
                       }}}
        if provider == 'openrouter':
            request['provider'] = {'require_parameters': True}
        # Some compatible backends support JSON mode but not json_schema.
        # Local Pydantic validation remains mandatory on both attempts.
        if attempt > 0:
            request['response_format'] = {'type': 'json_object'}
        await report('model_wait', purpose=purpose, attempt=attempt + 1)
        try:
            session = await self._get_session()
            async with session.post(endpoint, headers=headers, json=request, allow_redirects=False) as response:
                status_label = f'http_{response.status}'
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16_384):
                    raw.extend(chunk)
                    if len(raw) > 131_072:
                        raise _Retryable('oversized_response')
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                usage = payload.get('usage') or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_tokens = int(_number(usage.get('prompt_tokens')))
                output_tokens = int(_number(usage.get('completion_tokens')))
                if usage.get('cost') is not None:
                    cost = _number(usage['cost'])
                    try:
                        supplied = float(usage['cost'])
                        valid = not isinstance(usage['cost'], bool) and math.isfinite(supplied) and supplied >= 0
                    except (TypeError, ValueError, OverflowError):
                        valid = False
                    # OpenRouter documents usage.cost in USD. Other backends must state currency explicitly.
                    if valid and (provider == 'openrouter' or usage.get('currency') == 'USD'):
                        cost_source, currency, actual_cost = 'provider', 'USD', supplied
                else:
                    cost = (input_tokens * self.settings.llm_input_cost_per_million +
                            output_tokens * self.settings.llm_output_cost_per_million) / 1_000_000
                    if (input_tokens or output_tokens) and cost > 0:
                        cost_source, currency = 'estimate', 'USD'
                if response.status in (401, 402, 403):
                    raise ProviderUnavailable('Сервис анализа недоступен: администратору нужно проверить ключ и баланс API.')
                if response.status != 200 or payload.get('error'):
                    raise _Retryable(status_label)
                try:
                    choice = payload['choices'][0]
                    content = choice['message']['content']
                    if not isinstance(content, str) or not content.strip() or choice.get('finish_reason') == 'length':
                        raise ValueError
                except (KeyError, IndexError, TypeError, ValueError):
                    raise _Retryable('invalid_response') from None
                status_label = 'ok'
                return content
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise _Retryable('transport_error') from None
        finally:
            if self.usage_callback:
                await self.usage_callback(operation='llm', provider=provider, model=model,
                    input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost=cost,
                    detail=f'{purpose}:{status_label}', cost_source=cost_source,
                    currency=currency, actual_cost=actual_cost)

    async def _complete(self, prompt, data, schema, purpose, validate=None):
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        messages = [
            {'role': 'system', 'content': prompt + '\nJSON Schema:\n' + schema_text},
            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)},
        ]
        for attempt in range(2):
            try:
                raw = await self._request(messages, schema, purpose, attempt)
                lines = raw.strip().splitlines()
                if len(lines) >= 3 and lines[0].lower() in {'```json', '```'} and lines[-1] == '```':
                    # Some compatible providers wrap otherwise valid JSON even
                    # in schema mode. Unwrap only one complete outer fence;
                    # never extract JSON from surrounding prose or skip validation.
                    raw = '\n'.join(lines[1:-1])
                result = schema.model_validate_json(raw, strict=True)
                if validate:
                    validate(result)
                return result
            except (_Retryable, ValidationError, ValueError) as exc:
                if attempt == 0:
                    # Feedback names the failed schema/rule, never input values
                    # or HTTP payloads. This lets the retry fix the actual error.
                    failure = ('; '.join('.'.join(map(str, e['loc'])) + ': ' + e['type']
                               for e in exc.errors(include_input=False)[:4])
                               if isinstance(exc, ValidationError) else
                               str(exc)[:250] if isinstance(exc, ValueError) else 'request_failed')
                    messages.append({'role': 'user', 'content':
                        'Повтори ответ строго по JSON Schema. Все поля обязательны; без markdown. '
                        'Проверь типы, лимиты и что доказательные URL точно взяты из sources. '
                        'При недостатке доказательств meaningful_update=false. '
                        'Ошибка локальной проверки: ' + failure})
                    await asyncio.sleep(0.5)
        raise ProviderUnavailable(AI_ERROR)

    async def extract(self, text: str, url: str | None = None) -> StoryExtraction:
        if not text or len(text.strip()) < 5:
            raise ProviderUnavailable('Пришлите более подробное описание сюжета или текст новости.')

        def validate(result):
            for key in ('entities', 'keywords', 'search_queries', 'watch_goals'):
                if any(not item.strip() or len(item) > 250 for item in getattr(result, key)):
                    raise ValueError('Invalid list item')

        return await self._complete(EXTRACT, {'seed_text': text[:18_000], 'seed_url': url,
            'current_time_utc': datetime.now(timezone.utc).isoformat()},
            StoryExtraction, 'extract', validate)

    async def analyze(self, story: dict, sources: list[Candidate]) -> Analysis:
        if not sources:
            raise ProviderUnavailable('Нет доступных новых источников для анализа.')
        selected = sources[:max(1, min(6, self.settings.max_sources_per_check))]
        allowed_urls = {source.url for source in selected}
        story_data = {key: story.get(key) for key in
                      ('title', 'current_state', 'watch_goals', 'known_facts', 'last_checked_at',
                       'last_meaningful_update_at', 'created_at', 'monitoring_started_at')}
        story_data['current_state'] = str(story_data.get('current_state') or '')[:4000]
        # Timestamp values may arrive from ORM serialization or a direct caller.
        for key, value in list(story_data.items()):
            if isinstance(value, datetime):
                story_data[key] = value.isoformat()
        source_data = [{
            'url': s.url, 'publisher_domain': s.domain, 'title': s.title[:400],
            'content': s.content_excerpt[:4500],
            'published_at': s.published_at.isoformat() if s.published_at else None,
            'full_text_verified': s.full_text,
        } for s in selected]

        def validate(result):
            if len(set(result.source_urls)) != len(result.source_urls) or not set(result.source_urls) <= allowed_urls:
                raise ValueError('Evidence is not in supplied candidates')
            if result.meaningful_update and (not result.relevant or not result.new_facts or
                    not result.source_urls or not result.updated_state.strip() or not result.notification_summary.strip()):
                raise ValueError('Meaningful update lacks evidence')
            if any(not fact.strip() or len(fact) > 500 for fact in result.new_facts):
                raise ValueError('Invalid fact')

        result = await self._complete(ANALYZE, {'story': story_data, 'sources': source_data,
            'current_time_utc': datetime.now(timezone.utc).isoformat()},
            Analysis, 'analyze', validate)
        if not (result.meaningful_update and result.relevant and result.novelty_score >= .65
                and result.importance_score >= .55 and result.confidence >= .75):
            return result
        bodies = {s['url']: s for s in source_data if s['full_text_verified']}
        if not bodies:
            result.meaningful_update = False
            result.confidence = min(result.confidence, .70)
            return result
        review_sources, passages = evidence_passages(list(bodies.values()))

        def validate_review(review):
            if not review.meaningful_update:
                validate(review)
                return
            covered = set()
            quoted_urls = []
            for proof in review.evidence:
                passage = passages.get(proof.passage_id)
                if passage is None or proof.fact_index >= len(review.new_facts):
                    raise ValueError('Evidence must reference a full-text source and a retained fact')
                covered.add(proof.fact_index)
                quoted_urls.append(passage['url'])
            if covered != set(range(len(review.new_facts))):
                raise ValueError('Every fact requires matching evidence')
            # Citations are deterministic consequences of validated references.
            # Never trust or retry a redundant URL list generated by the model.
            review.source_urls = list(dict.fromkeys(quoted_urls))
            validate(review)

        await report('verifying', sources=len(bodies))
        # The editor checks support and attribution, not a second, narrower
        # relevance decision based on possibly over-specific original goals.
        review_story = {key: value for key, value in story_data.items() if key != 'watch_goals'}
        reviewed = await self._complete(VERIFY, {'story': review_story, 'sources': review_sources,
            'proposal': result.model_dump()}, ReviewedAnalysis, 'verify', validate_review)
        return Analysis.model_validate(reviewed.model_dump(exclude={'evidence'}))

    async def close(self):
        if self._session is not None:
            await self._session.close()
