import asyncio
import gzip
import json
import socket
import zlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from defusedxml.common import DefusedXmlException

from app.ai import AIClient
from app.content.fetcher import MAX_BYTES, ContentFetcher, PublicResolver, extract_content, read_bounded_body, validate_url
from app.domain import Candidate, ProviderUnavailable, UserError
from app.search import SearchClient
from app.search.client import parse_bing_rss, parse_date, parse_google_rss
from app.search.dedupe import content_hash, is_near_duplicate, normalize_url


def settings(**changes):
    values = dict(llm_provider='openrouter', llm_api_key='test-key', llm_model='test-model',
                  llm_base_url='', llm_timeout_seconds=10, llm_input_cost_per_million=0.1,
                  llm_output_cost_per_million=0.4, max_sources_per_check=6,
                  max_results_per_query=4, search_provider='google_news', search_api_key='')
    return SimpleNamespace(**(values | changes))


class Response:
    def __init__(self, payload=b'', status=200, headers=None):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {}
        self.content_length = len(self.payload)
        self.charset = 'utf-8'
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for start in range(0, len(self.payload), size):
            yield self.payload[start:start + size]


class Session:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)

    def request(self, method, url, **kwargs):
        return self.post(url, **kwargs)


EXTRACTION = dict(title='Тестовый запуск', short_summary='Продукт ещё не запущен.',
    current_state='Дата запуска неизвестна.', entities=['Продукт'], keywords=['запуск'],
    search_queries=['product release', 'product announcement'], watch_goals=['Дата запуска'])

ANALYSIS = dict(relevant=True, meaningful_update=True, novelty_score=0.9,
    importance_score=0.8, confidence=0.9, new_facts=['Названа дата запуска.'],
    updated_state='Запуск назначен на завтра.', reason='Появилась дата.',
    notification_summary='Объявлена дата запуска.', source_urls=['https://example.com/story'])


def completion(data, cost=0.001):
    return Response({'choices': [{'message': {'content': json.dumps(data)}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 20, 'completion_tokens': 30, 'cost': cost}})


def candidate():
    return Candidate(url='https://example.com/story', normalized_url='https://example.com/story',
        domain='example.com', title='Дата запуска', content_excerpt='Официальное объявление даты.',
        content_hash='abc', published_at=datetime.now(timezone.utc))


@pytest.mark.parametrize('url', [
    'http://127.0.0.1/', 'http://127.1/', 'http://2130706433/', 'http://0177.0.0.1/',
    'http://169.254.169.254/latest/meta-data', 'http://10.2.3.4/', 'http://192.168.1.1',
    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://localhost/',
    'http://foo.local/', 'https://public.com:8080/', 'https://user:pass@example.com/',
    'file:///etc/passwd', 'http://[fe80::1%25eth0]/', 'http://example.com\\@127.0.0.1/',
    'https://example.com/\nanything',
    'http://127。0。0。1/', 'http://127.0.0.1\u00ad/', 'http://１２７.０.０.１/',
])
def test_ssrf_unsafe_urls_rejected(url):
    with pytest.raises(UserError):
        validate_url(url)


def test_safe_url_and_normalization():
    assert validate_url('https://example.com/a#b') == 'https://example.com/a'
    assert normalize_url('HTTPS://EXAMPLE.COM:443/a?b=2&utm_source=x&a=1#part') == 'https://example.com/a?a=1&b=2'
    assert content_hash('A Test!') == content_hash('a  test')
    assert is_near_duplicate('Launch on September 30', ['Launch on September 30'])
    assert not is_near_duplicate('Launch on September 30', ['Launch on October 30'])


@pytest.mark.asyncio
async def test_dns_rebinding_and_mixed_answer_rejected(monkeypatch):
    resolver = PublicResolver()
    lookup = AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
    ])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    with pytest.raises(OSError):
        await resolver.resolve('attacker.example', 443)
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_resolver_pins_public_numeric_answers(monkeypatch):
    lookup = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    rows = await PublicResolver().resolve('example.com', 443)
    assert rows[0]['host'] == '93.184.216.34'
    assert rows[0]['flags'] == socket.AI_NUMERICHOST
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_redirect_to_private_host_blocked_before_second_request():
    fetcher = ContentFetcher()
    fetcher._session = Session([Response(status=302, headers={'Location': 'http://169.254.169.254/'})])
    with pytest.raises(UserError):
        await fetcher.fetch('https://example.com/story')
    assert len(fetcher._session.calls) == 1


@pytest.mark.asyncio
async def test_content_size_limit_and_unreadable_html():
    fetcher = ContentFetcher()
    fetcher._session = Session([Response(b'x' * 1_048_577, headers={'Content-Type': 'text/html'})])
    with pytest.raises(UserError):
        await fetcher.fetch('https://example.com/')
    with pytest.raises(UserError):
        extract_content('https://example.com', b'<h1>Blocked</h1>', 'text/html')


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding', ['gzip', 'deflate', 'raw-deflate'])
async def test_compressed_article_fetch_extracts_actual_text(encoding):
    article = b'<html><title>Release</title><article>' + b'The release is confirmed. ' * 20 + b'</article></html>'
    if encoding == 'gzip':
        compressed = gzip.compress(article)
    elif encoding == 'deflate':
        compressed = zlib.compress(article)
    else:
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        compressed = compressor.compress(article) + compressor.flush()
    fetcher = ContentFetcher()
    fetcher._session = Session([Response(compressed, headers={
        'Content-Type': 'text/html', 'Content-Encoding': 'deflate' if encoding == 'raw-deflate' else encoding})])
    result = await fetcher.fetch('https://example.com/release')
    assert result.title == 'Release'
    assert result.text == ('The release is confirmed. ' * 20).strip()


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
async def test_compression_bomb_rejected_before_unbounded_output(encoding, compress):
    compressed = compress(b'a' * (MAX_BYTES * 8))
    assert len(compressed) < MAX_BYTES
    response = Response(compressed, headers={'Content-Encoding': encoding})
    with pytest.raises(UserError):
        await read_bounded_body(response)


@pytest.mark.asyncio
@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
async def test_truncated_compressed_body_rejected_even_when_text_is_complete(encoding, compress):
    compressed = compress(b'Complete-looking article body. ' * 40)
    response = Response(compressed[:-4], headers={'Content-Encoding': encoding})
    with pytest.raises(UserError):
        await read_bounded_body(response)


@pytest.mark.asyncio
async def test_encoded_transfer_limit_enforced_without_content_length():
    response = Response(b'x' * (MAX_BYTES + 1))
    response.content_length = None
    with pytest.raises(UserError):
        await read_bounded_body(response)


@pytest.mark.asyncio
async def test_unsupported_or_corrupt_compression_rejected():
    for payload, encoding in [(b'anything', 'br'), (b'invalid gzip', 'gzip'),
                              (gzip.compress(b'news') + b'trailing garbage', 'gzip')]:
        with pytest.raises(UserError):
            await read_bounded_body(Response(payload, headers={'Content-Encoding': encoding}))


def test_visible_article_extraction_strips_scripts():
    source = ('<html><title>News</title><script>steal secret</script><nav>Navigation</nav>'
              '<article>' + 'Actual article text. ' * 12 + '</article></html>').encode()
    fetched = extract_content('https://example.com', source, 'text/html')
    assert fetched.title == 'News'
    assert 'Actual article' in fetched.text
    assert 'secret' not in fetched.text
    assert 'Navigation' not in fetched.text


def test_rss_timestamp_and_untrusted_excerpt_marker():
    rss = b'''<rss><channel><item><title>Launch - Publisher</title>
        <link>https://news.google.com/rss/articles/test</link>
        <pubDate>Wed, 30 Sep 2026 12:00:00 GMT</pubDate><source>Publisher</source>
        <description>&lt;b&gt;Launch confirmed&lt;/b&gt;</description></item></channel></rss>'''
    results = parse_google_rss(rss, 'launch', 4)
    assert len(results) == 1
    assert results[0].published_at == datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    assert 'полный текст не проверен' in results[0].snippet
    assert 'Publisher' in results[0].snippet
    assert parse_date('2 hours ago') is None  # Never fabricate publication dates.


def test_rss_external_entities_rejected():
    xml = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>&x;</channel></rss>'
    with pytest.raises(DefusedXmlException):
        parse_google_rss(xml, 'test', 4)


def test_bing_rss_unwraps_publisher_url_and_keeps_publication_date():
    xml = b'''<rss xmlns:News="urn:news"><channel><item><title>Launch</title>
        <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3A%2F%2Fpublisher.example%2Fnews%3Fa%3D1</link>
        <pubDate>Wed, 30 Sep 2026 13:00:00 GMT</pubDate><description>Article details.</description>
        <News:Source>Publisher</News:Source></item><item><title>Bad</title>
        <link>http://www.bing.com/news/apiclick.aspx?url=http%3A%2F%2F127.1%2F</link>
        </item></channel></rss>'''
    results = parse_bing_rss(xml, 'launch', 4)
    assert len(results) == 1
    assert results[0].url == 'https://publisher.example/news?a=1'
    assert results[0].published_at == datetime(2026, 9, 30, 13, tzinfo=timezone.utc)
    assert 'Publisher' in results[0].snippet


@pytest.mark.asyncio
async def test_bing_localizes_russian_query_and_counts_one_search():
    callback = AsyncMock()
    client = SearchClient(settings(search_provider='bing_news'), callback)
    client._session = Session([Response(b'<rss><channel/></rss>')])
    assert await client.search('Дата запуска') == []
    assert client._session.calls[0][1]['params']['mkt'] == 'ru-RU'
    assert callback.await_count == 1


@pytest.mark.asyncio
async def test_search_failure_still_counts_attempt():
    callback = AsyncMock()
    search = SearchClient(settings(), callback)
    search._session = Session([Response(status=503)])
    with pytest.raises(ProviderUnavailable):
        await search.search('release date')
    assert callback.await_count == 1
    assert callback.call_args.kwargs['operation'] == 'search'


@pytest.mark.asyncio
async def test_real_structure_and_actual_cost_recorded():
    callback = AsyncMock()
    client = AIClient(settings(), callback)
    client._session = Session([completion(EXTRACTION, cost=0.002)])
    result = await client.extract('Следи за датой запуска нового продукта.')
    assert result.title == EXTRACTION['title']
    assert [c.kwargs['operation'] for c in callback.call_args_list] == ['llm_attempt', 'llm']
    assert callback.call_args.kwargs['estimated_cost'] == 0.002
    assert callback.call_args.kwargs['input_tokens'] == 20
    assert client._session.calls[0][1]['allow_redirects'] is False


@pytest.mark.asyncio
async def test_budget_rejection_makes_no_network_call():
    callback = AsyncMock(side_effect=UserError('Лимит исчерпан'))
    client = AIClient(settings(), callback)
    client._session = Session([])
    with pytest.raises(UserError, match='Лимит'):
        await client.extract('Следи за датой запуска продукта.')
    assert not client._session.calls
    assert callback.await_count == 1


@pytest.mark.asyncio
async def test_no_fabricated_fallback_and_both_failed_attempts_counted():
    callback = AsyncMock()
    client = AIClient(settings(), callback)
    client._session = Session([completion({'title': 'Missing fields'}), completion({'title': 'Missing fields'})])
    with pytest.raises(ProviderUnavailable):
        await client.extract('Следи за датой запуска нового продукта.')
    assert [c.kwargs['operation'] for c in callback.call_args_list] == ['llm_attempt', 'llm'] * 2


@pytest.mark.asyncio
async def test_citations_must_be_exact_candidate_urls():
    client = AIClient(settings())
    client._session = Session([completion(ANALYSIS | {'source_urls': ['https://invented.example/']})] * 2)
    with pytest.raises(ProviderUnavailable):
        await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])


@pytest.mark.asyncio
async def test_valid_analysis_includes_known_state_and_sources():
    client = AIClient(settings())
    client._session = Session([completion(ANALYSIS)])
    result = await client.analyze({'current_state': 'Дата неизвестна', 'known_facts': ['Анонс уже был.']}, [candidate()])
    assert result.meaningful_update
    payload = client._session.calls[0][1]['json']
    assert 'known_facts' in payload['messages'][1]['content']
    assert 'Анонс уже был.' in payload['messages'][1]['content']
    assert 'недоверенные' in payload['messages'][0]['content']


@pytest.mark.asyncio
async def test_disabled_provider_is_explicit():
    client = AIClient(settings(llm_provider='disabled'))
    with pytest.raises(ProviderUnavailable, match='API-ключ'):
        await client.extract('Следи за новым продуктом.')
