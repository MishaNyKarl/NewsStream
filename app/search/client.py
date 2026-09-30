import asyncio
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import aiohttp
from bs4 import BeautifulSoup
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from app.domain import ProviderUnavailable, SearchResult, SearchResults
from app.content.fetcher import validate_url
from app.domain import UserError
from app.search.dedupe import normalize_url

SEARCH_ERROR = 'Поиск временно недоступен. Проверка будет повторена позже.'
MAX_RESPONSE = 1_048_576


def parse_date(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (TypeError, ValueError, OverflowError):
            return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)


def plain_html(value: str) -> str:
    return BeautifulSoup(value, 'html.parser').get_text(' ', strip=True)


def parse_google_rss(body: bytes, query: str, limit: int) -> list[SearchResult]:
    root = ElementTree.fromstring(body)
    results = []
    for item in root.findall('./channel/item'):
        url = (item.findtext('link') or '').strip()
        title = plain_html(item.findtext('title') or '')[:400]
        if not url or not title or urlsplit(url).scheme not in ('http', 'https'):
            continue
        description = plain_html(item.findtext('description') or '')[:1600]
        source = item.findtext('source') or ''
        source_node = item.find('source')
        publisher_domain = ''
        if source_node is not None:
            try:
                publisher_domain = urlsplit(validate_url(source_node.get('url') or '')).hostname or ''
            except UserError:
                pass
        # RSS supplies a headline, not a verified article body. Preserve that
        # limitation all the way into the LLM context.
        snippet = f'[Только заголовок/краткий фрагмент RSS; полный текст не проверен.] {description}'
        if source:
            snippet += f' Источник: {source[:150]}.'
        results.append(SearchResult(url=url, title=title, snippet=snippet,
                                    published_at=parse_date(item.findtext('pubDate')), query=query,
                                    publisher_domain=publisher_domain))
        if len(results) >= limit:
            break
    return results


def parse_bing_rss(body: bytes, query: str, limit: int) -> list[SearchResult]:
    root = ElementTree.fromstring(body)
    results = []
    for item in root.findall('./channel/item'):
        url = (item.findtext('link') or '').strip()
        parts = urlsplit(url)
        if (parts.hostname or '').lower() in ('bing.com', 'www.bing.com'):
            # Bing RSS provides a publisher URL inside its attribution link.
            # Decode the query exactly once and apply the same URL policy as
            # article fetching. DNS is checked later by the pinned resolver.
            url = (parse_qs(parts.query).get('url') or [''])[0]
        try:
            url = validate_url(url)
        except UserError:
            continue
        title = plain_html(item.findtext('title') or '')[:400]
        if not title:
            continue
        description = plain_html(item.findtext('description') or '')[:1600]
        source = next((child.text or '' for child in item
                       if child.tag.rsplit('}', 1)[-1].lower() == 'source'), '')
        snippet = '[Фрагмент поиска/RSS; полный текст не проверен.] ' + description
        if source:
            snippet += f' Источник: {source[:150]}.'
        results.append(SearchResult(url=url, title=title, snippet=snippet,
                                    published_at=parse_date(item.findtext('pubDate')), query=query))
        if len(results) >= limit:
            break
    return results


class SearchClient:
    def __init__(self, settings, usage_callback=None):
        self.settings = settings
        self.usage_callback = usage_callback
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20), trust_env=False,
                headers={'User-Agent': 'NewsWatchMVP/1.0'},
            )
        return self._session

    async def _read(self, method, url, **kwargs):
        session = await self._get_session()
        async with session.request(method, url, allow_redirects=False, **kwargs) as response:
            if response.status != 200:
                raise ProviderUnavailable(SEARCH_ERROR)
            if response.content_length is not None and response.content_length > MAX_RESPONSE:
                raise ProviderUnavailable(SEARCH_ERROR)
            data = bytearray()
            async for chunk in response.content.iter_chunked(32_768):
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise ProviderUnavailable(SEARCH_ERROR)
            return bytes(data)

    async def search(self, query: str) -> list[SearchResult]:
        if self.settings.search_provider != 'hybrid_news':
            provider = self.settings.search_provider
            return SearchResults(await self._search(query, provider), providers=(provider,))
        providers = ('bing_news', 'google_news')
        batches = await asyncio.gather(*(self._search(query, provider) for provider in providers), return_exceptions=True)
        failed = [provider for provider, rows in zip(providers, batches) if isinstance(rows, Exception)]
        if len(failed) == len(providers):
            raise ProviderUnavailable(SEARCH_ERROR)
        results = []
        seen = set()
        # Interleave providers, so one provider cannot consume all early slots.
        valid = [rows for rows in batches if not isinstance(rows, Exception)]
        for index in range(max((len(rows) for rows in valid), default=0)):
            for rows in valid:
                if index < len(rows):
                    result = rows[index]
                    key = normalize_url(result.url)
                    if key not in seen:
                        results.append(result)
                        seen.add(key)
        return SearchResults(results, providers=providers, failed_providers=failed)

    async def _search(self, query: str, provider: str) -> list[SearchResult]:
        import json
        query = ' '.join(query.split())[:350]
        if not query:
            return []
        limit = max(1, min(10, self.settings.max_results_per_query))
        if provider not in ('google_news', 'bing_news', 'brave', 'tavily'):
            raise ProviderUnavailable('Администратору нужно настроить поискового провайдера.')
        if provider not in ('google_news', 'bing_news') and not self.settings.search_api_key:
            raise ProviderUnavailable('Администратору нужно добавить ключ поискового провайдера.')
        if self.usage_callback:
            await self.usage_callback(operation='search', provider=provider, detail=query)
        try:
            if provider == 'bing_news':
                locale = 'ru-RU' if re.search('[А-Яа-яЁё]', query) else 'en-US'
                body = await self._read('GET', 'https://www.bing.com/news/search',
                    params={'q': query, 'format': 'rss', 'mkt': locale, 'count': limit, 'sortbydate': '1'})
                results = parse_bing_rss(body, query, limit)
            elif provider == 'google_news':
                days = max(1, min(14, getattr(self.settings, 'search_recent_days', 7)))
                effective = query if 'when:' in query else f'{query} when:{days}d'
                locale = {'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'} if re.search('[А-Яа-яЁё]', query) else {
                    'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'}
                body = await self._read('GET', 'https://news.google.com/rss/search',
                    params={'q': effective, **locale})
                results = parse_google_rss(body, query, limit)
            elif provider == 'brave':
                body = await self._read('GET', 'https://api.search.brave.com/res/v1/news/search',
                    params={'q': query, 'count': limit, 'freshness': 'pw'},
                    headers={'X-Subscription-Token': self.settings.search_api_key})
                payload = json.loads(body)
                results = [SearchResult(url=r.get('url', ''), title=r.get('title', '')[:400],
                    snippet='[Фрагмент поиска; полный текст не проверен.] ' + r.get('description', '')[:1600],
                    published_at=parse_date(r.get('meta_url', {}).get('published') or r.get('page_age')),
                    query=query) for r in payload.get('results', [])[:limit]]
            else:
                body = await self._read('POST', 'https://api.tavily.com/search', json={
                    'api_key': self.settings.search_api_key, 'query': query, 'topic': 'news',
                    'days': 7, 'max_results': limit, 'search_depth': 'basic',
                    'include_answer': False, 'include_raw_content': False,
                })
                payload = json.loads(body)
                results = [SearchResult(url=r.get('url', ''), title=r.get('title', '')[:400],
                    snippet='[Фрагмент поиска; полный текст не проверен.] ' + r.get('content', '')[:1600],
                    published_at=parse_date(r.get('published_date')), query=query)
                    for r in payload.get('results', [])[:limit]]
            seen = set()
            valid = []
            for result in results:
                if not result.url or not result.title or urlsplit(result.url).scheme not in ('http', 'https'):
                    continue
                key = normalize_url(result.url)
                if key not in seen:
                    seen.add(key)
                    valid.append(result)
            return valid
        except ProviderUnavailable:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError,
                KeyError, AttributeError, ElementTree.ParseError, DefusedXmlException):
            raise ProviderUnavailable(SEARCH_ERROR) from None

    async def close(self):
        if self._session is not None:
            await self._session.close()
