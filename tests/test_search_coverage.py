from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.bot import notification_text
from app.content.fetcher import extract_content
from app.domain import Candidate, ProviderUnavailable, SearchResult, SearchResults, UserError
from app.progress import CheckMetrics, CheckOutcome
from app.search.client import SearchClient, parse_google_rss
from app.search.dedupe import content_hash
from app.search.planning import diverse_results, plan_queries, source_cutoff
from app.telegram_progress import outcome_text
from test_pipeline import Response, Session, settings
from test_service import analysis, setup_service, story


def rss(url, title='Release'):
    return f'<rss><channel><item><title>{title}</title><link>{url}</link></item></channel></rss>'.encode()


async def test_hybrid_search_combines_sources_and_counts_actual_requests():
    usage = AsyncMock()
    client = SearchClient(settings(search_provider='hybrid_news'), usage)
    client._session = Session([Response(rss('https://a.example/new')), Response(rss('https://b.example/new'))])
    results = await client.search('release')
    assert [r.url for r in results] == ['https://a.example/new', 'https://b.example/new']
    assert results.providers == ('bing_news', 'google_news') and not results.failed_providers
    assert {c.kwargs['provider'] for c in usage.call_args_list} == {'bing_news', 'google_news'}
    assert usage.await_count == 2


async def test_hybrid_cross_provider_duplicate_is_once():
    client = SearchClient(settings(search_provider='hybrid_news'))
    client._session = Session([Response(rss('https://a.example/new')), Response(rss('https://a.example/new?utm_source=rss'))])
    assert len(await client.search('release')) == 1


async def test_partial_hybrid_search_keeps_working_source_but_reports_failure():
    client = SearchClient(settings(search_provider='hybrid_news'))
    client._session = Session([Response(status=503), Response(rss('https://a.example/new'))])
    results = await client.search('release')
    assert len(results) == 1 and results.failed_providers == ('bing_news',)
    client._session = Session([Response(status=503), Response(status=503)])
    with pytest.raises(ProviderUnavailable):
        await client.search('release')


async def test_google_russian_locale_and_bounded_recency():
    client = SearchClient(settings(search_recent_days=99))
    client._session = Session([Response(b'<rss><channel/></rss>')])
    await client.search('Flydubai новости')
    params = client._session.calls[0][1]['params']
    assert params == {'q': 'Flydubai новости when:14d', 'hl': 'ru', 'gl': 'RU', 'ceid': 'RU:ru'}


@pytest.mark.parametrize('source,expected', [('https://www.reuters.com', 'www.reuters.com'), ('http://127.0.0.1', '')])
def test_google_publisher_metadata_does_not_turn_into_an_article_url(source, expected):
    xml = rss('https://news.google.com/rss/articles/item').replace(b'</item>', f'<source url="{source}">Reuters</source></item>'.encode())
    result = parse_google_rss(xml, 'release', 8)[0]
    assert result.publisher_domain == expected
    assert result.url == 'https://news.google.com/rss/articles/item'
    assert 'полный текст не проверен' in result.snippet


def test_existing_narrow_story_gets_broad_bilingual_queries_without_inventing_facts():
    item = story()
    item.title = 'Попытка аварийного снижения самолёта Flydubai'
    item.entities = ['Flydubai', 'пилот']
    item.search_queries = ['Flydubai pilot incident 2026', 'Flydubai cockpit fight pilot knife', 'Flydubai plane dive attempt']
    result = plan_queries(item, Settings(_env_file=None, max_search_queries_per_story=6, search_expand_queries=True))
    assert len(result) == 6 and result[:3] == item.search_queries
    assert item.title in result and 'Flydubai новости' in result and 'Flydubai latest news' in result
    assert all('FZ1073' not in q and 'Tabuk' not in q for q in result)


def test_source_window_recovers_recent_background_without_unbounded_archive():
    now = datetime.now(timezone.utc)
    item = story()
    item.created_at = now
    config = Settings(_env_file=None)
    assert source_cutoff(item, config, now) == now - timedelta(hours=48)
    item.created_at = now - timedelta(days=60)
    assert source_cutoff(item, config, now) == now - timedelta(days=7)


def test_publisher_diversity_uses_google_publisher_not_aggregator_domain():
    now = datetime.now(timezone.utc)
    rows = [SearchResult(f'https://news.google.com/{i}', f'News {i}', publisher_domain=domain,
                         published_at=now) for i, domain in enumerate(['a.example', 'a.example', 'b.example', 'reuters.com'])]
    result = diverse_results(rows, now)
    assert [r.publisher_domain for r in result[:3]] == ['reuters.com', 'a.example', 'b.example']


def test_google_wrappers_cannot_starve_direct_article_reading_budget():
    now = datetime.now(timezone.utc)
    wrappers = [SearchResult(f'https://news.google.com/{i}', f'Latest {i}', publisher_domain=f'publisher{i}.example',
                            published_at=now) for i in range(20)]
    direct = SearchResult('https://example.org/landing', 'Confirmed landing', published_at=now-timedelta(hours=4))
    assert diverse_results(wrappers + [direct], now-timedelta(minutes=30))[0] is direct


async def test_recent_pre_subscription_article_reaches_analysis_and_reports_coverage():
    service, item, metrics = setup_service(), story(), CheckMetrics()
    now = datetime.now(timezone.utc)
    item.created_at = now - timedelta(minutes=30)
    service.search.search.return_value = SearchResults([
        SearchResult('https://example.org/release', 'Landing', published_at=item.created_at - timedelta(hours=2)),
        SearchResult('https://example.org/ancient', 'Archive', published_at=now - timedelta(days=10)),
        SearchResult('https://example.org/future', 'Future', published_at=now + timedelta(days=1)),
    ], providers=('bing_news', 'google_news'), failed_providers=('google_news',))
    service.fetcher.fetch.return_value = SimpleNamespace(url='https://example.org/release', title='Landing', text='Everyone landed safely. ' * 12)
    candidates = await service._candidates(item, metrics)
    assert len(candidates) == 1 and candidates[0].full_text
    assert metrics.before_subscription == 1 and metrics.outside_window == 2
    assert metrics.queries == 2 and metrics.queries_failed == 1 and metrics.results == 3
    assert metrics.full_texts == 1 and metrics.snippets == 0


async def test_full_texts_preferred_and_source_read_budget_is_bounded():
    service, item, metrics = setup_service(), story(), CheckMetrics()
    service.search.search.return_value = [SearchResult(f'https://p{i}.example/{i}', f'Publication {i}') for i in range(15)]
    async def fetch(url):
        number = int(url.rsplit('/', 1)[-1])
        if number < 3:
            raise OSError('blocked')
        return SimpleNamespace(url=url, title=f'Publication {number}', text=f'Passenger count {number}. ' * 15)
    service.fetcher.fetch.side_effect = fetch
    candidates = await service._candidates(item, metrics)
    assert service.fetcher.fetch.await_count == 10
    assert len(candidates) == 6 and all(c.full_text for c in candidates)
    assert metrics.full_texts == 6 and metrics.snippets == 0


@pytest.mark.parametrize('changed,blocked,minutes,expected', [(True, False, 20, 1), (False, False, 20, 0), (True, True, 20, 0), (True, False, 2, 0)])
async def test_known_url_is_rechecked_for_changed_body_only_after_cooldown(changed, blocked, minutes, expected):
    service, item = setup_service(), story()
    url, title = 'https://example.org/release', 'Landing'
    body = 'Passengers are waiting at the airport. ' * 15
    old = title + '\n' + body
    service.repo.known_sources.return_value = [SimpleNamespace(normalized_url=url, content_hash=content_hash(old),
        content_excerpt=old, fetched_at=datetime.now(timezone.utc)-timedelta(minutes=minutes))]
    service.repo.known_url_set.return_value = {url}
    service.search.search.return_value = [SearchResult(url, title, snippet='RSS new fragment')]
    if blocked:
        service.fetcher.fetch.side_effect = OSError('blocked')
    else:
        service.fetcher.fetch.return_value = SimpleNamespace(url=url, title=title, text=body + (' All 150 passengers are safe.' if changed else ''))
    assert len(await service._candidates(item)) == expected
    assert service.fetcher.fetch.await_count == (0 if minutes == 2 else 1)


async def test_snippet_only_evidence_cannot_notify_even_when_model_claims_high_confidence():
    service = setup_service()
    item = Candidate('https://example.org/release', 'https://example.org/release', 'example.org',
                     'Release', 'Headline fragment', 'hash', full_text=False)
    service._candidates = AsyncMock(return_value=[item])
    service.ai.analyze.return_value = analysis(confidence=.99)
    service.repo.save_check.return_value = None
    service.repo.finish_check.return_value = True
    result = await service.check_story(story())
    saved = service.repo.save_check.call_args.args[3]
    assert saved.confidence == .70 and not saved.meaningful_update
    assert result.status == 'unverified'
    assert 'полные тексты недоступны' in outcome_text(result, 4)
    # An unrelated readable page cannot lend confidence to snippet-only citations.
    service._candidates.return_value = [item, replace(item, url='https://other.example/article', full_text=True)]
    service.ai.analyze.return_value = analysis(confidence=.99)
    await service.check_story(story())
    assert not service.repo.save_check.call_args.args[3].meaningful_update


def test_bot_challenge_is_not_counted_as_readable_article():
    html = '<title>Just a moment...</title><article>' + ('Enable JavaScript and cookies to continue. ' * 12) + '</article>'
    with pytest.raises(UserError):
        extract_content('https://example.org', html.encode(), 'text/html')


def test_context_notice_and_search_counts_are_clear_and_html_safe():
    item = SimpleNamespace(title='<Aircraft>')
    update = SimpleNamespace(update_kind='context', is_demo=False, summary='Landing <confirmed>',
        new_facts=['Passengers are safe'], reason='Outcome known', source_urls=['https://example.org/landing'])
    rendered = notification_text(item, update)
    assert 'Уточнение исходной новости' in rendered and 'Что уточнилось' in rendered
    assert 'после подписки не подтверждено' in rendered and '&lt;Aircraft&gt;' in rendered
    summary = outcome_text(CheckOutcome('changed', sources=6, update_kind='context', search_summary={
        'providers': ['bing_news', 'google_news'], 'queries': 12, 'results': 73, 'full_texts': 4, 'snippets': 2}), 90)
    assert 'Bing News, Google News' in summary and 'запросов: 12' in summary
    assert 'Полных текстов: 4 · фрагментов: 2' in summary
    assert 'Найдено уточнение' in summary


async def test_known_source_rechecks_cannot_consume_entire_read_budget():
    service, item = setup_service(), story()
    rows = [SearchResult(f'https://p{i}.example/{i}', f'Publication {i}') for i in range(5)]
    service.search.search.return_value = rows
    service.repo.known_url_set.return_value = {r.url for r in rows}
    service.repo.known_sources.return_value = [SimpleNamespace(normalized_url=r.url, content_hash=f'old-{i}',
        content_excerpt=f'Old text {i}', fetched_at=datetime.now(timezone.utc)-timedelta(hours=1)) for i,r in enumerate(rows)]
    service.fetcher.fetch.side_effect = lambda url: SimpleNamespace(url=url, title=url, text=f'New details {url}. ' * 8)
    assert len(await service._candidates(item)) == 2
    assert service.fetcher.fetch.await_count == 2
