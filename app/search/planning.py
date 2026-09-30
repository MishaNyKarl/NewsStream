"""Bounded query expansion and publisher diversity without additional AI calls."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def plan_queries(story, settings):
    limit = max(1, min(6, settings.max_search_queries_per_story))
    saved = list(story.search_queries or [])
    if not settings.search_expand_queries:
        return saved[:limit]
    # Keep a broad query in the input language and a named-entity query. These
    # do not assume that the rumour in a narrow seed query has been confirmed.
    anchor = next((str(value).strip()[:80] for value in story.entities or [] if str(value).strip()), '')
    broad = [str(story.title)[:180]]
    if anchor:
        broad += [f'{anchor} новости']
        if re.search('[A-Za-z]', anchor):
            broad += [f'{anchor} latest news']
    choices = saved[:max(1, limit - min(3, len(broad)))] + broad + saved
    result, seen = [], set()
    for query in choices:
        query = ' '.join(query.split())[:250]
        key = query.casefold()
        if query and key not in seen:
            result.append(query)
            seen.add(key)
        if len(result) == limit:
            break
    return result


def source_cutoff(story, settings, now=None):
    now = now or datetime.now(timezone.utc)
    created = story.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(created - timedelta(hours=max(0, min(168, settings.search_lookback_hours))),
               now - timedelta(days=max(1, min(14, settings.search_recent_days))))


def publisher(result):
    return (result.publisher_domain or urlsplit(result.url).hostname or '').lower().removeprefix('www.')


def diverse_results(results, started_at):
    preferred = {'reuters.com', 'apnews.com', 'bbc.com', 'bbc.co.uk', 'afp.com'}
    def priority(result):
        stamp = result.published_at
        return (0 if publisher(result) in preferred else 1,
                0 if stamp and stamp >= started_at else 1,
                -(stamp.timestamp() if stamp else 0))
    ordered = sorted(results, key=priority)
    # First one per publisher, then second articles. This avoids filling the
    # analysis budget with a single publisher's variations on the same story.
    counts, first, rest = {}, [], []
    for result in ordered:
        domain = publisher(result)
        counts[domain] = counts.get(domain, 0) + 1
        (first if counts[domain] == 1 else rest).append(result)
    # Google RSS wrapper URLs commonly require JavaScript and return only a
    # headline. Do not let their fresher timestamps consume the entire reading
    # budget before accessible publisher pages have been attempted.
    ordered = first + rest
    def wrapper(result):
        return (urlsplit(result.url).hostname or '').lower() in {'news.google.com', 'consent.google.com'}
    return [result for result in ordered if not wrapper(result)] + [result for result in ordered if wrapper(result)]
