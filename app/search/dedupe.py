import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = {'fbclid', 'gclid', 'dclid', 'mc_cid', 'mc_eid', 'yclid', '_ga', 'ref_src'}


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or '').lower().rstrip('.')
    if ':' in host:
        host = f'[{host}]'
    port = parts.port
    if port and not ((parts.scheme.lower() == 'https' and port == 443) or
                     (parts.scheme.lower() == 'http' and port == 80)):
        host = f'{host}:{port}'
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith('utm_') and k.lower() not in TRACKING)
    return urlunsplit((parts.scheme.lower(), host, parts.path or '/', urlencode(query), ''))


def _normalized(text: str) -> str:
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def content_hash(text: str) -> str:
    return hashlib.sha256(_normalized(text).encode('utf-8')).hexdigest()


def is_near_duplicate(text: str, previous: list[str]) -> bool:
    words = _normalized(text).split()
    canonical = ' '.join(words)
    if not words:
        return False
    grams = set(tuple(words[i:i + 5]) for i in range(max(1, len(words) - 4)))
    for old in previous:
        old_words = _normalized(old).split()
        if canonical == ' '.join(old_words):
            return True
        # Very short headlines can differ by a single critical word or date.
        if len(words) < 30 or len(old_words) < 30:
            continue
        if re.findall(r'\d+', canonical) != re.findall(r'\d+', ' '.join(old_words)):
            continue
        old_grams = set(tuple(old_words[i:i + 5]) for i in range(len(old_words) - 4))
        union = grams | old_grams
        if union and len(grams & old_grams) / len(union) >= 0.94:
            return True
    return False
