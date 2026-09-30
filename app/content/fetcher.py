"""Bounded, public-network-only article fetching.

DNS answers are validated inside the resolver that the connector actually uses,
so validation and connection cannot resolve a hostname to different addresses.
Every redirect is separately validated. Proxy environment variables are ignored.
"""
import asyncio
import ipaddress
import socket
import zlib
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from bs4 import BeautifulSoup

from app.domain import FetchedContent, UserError

MAX_BYTES = 1_048_576
FETCH_ERROR = 'Не удалось прочитать страницу. Пришлите текст новости или кратко опишите сюжет.'


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_unspecified:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return is_public_address(str(address.ipv4_mapped))
        if address.sixtofour or address.teredo:
            return False
    return True


def validate_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise UserError(FETCH_ERROR)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if parts.scheme.lower() not in ('http', 'https') or not host:
            raise ValueError
        if parts.username is not None or parts.password is not None or port not in (None, 80, 443):
            raise ValueError
        if '\\' in parts.netloc or '%' in host:
            raise ValueError
        # Match the HTTP client's IDNA normalization before testing literals;
        # Unicode full stops or ignored characters can otherwise become a
        # private numeric IP only after validation.
        host = host.encode('idna').decode('ascii').rstrip('.').lower()
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            # aiohttp treats decimal/shorthand numeric hosts as literals and
            # bypasses its resolver. Reject anything ipaddress cannot parse.
            if host.replace('.', '').isdigit() or ':' in host:
                raise ValueError
        else:
            if not is_public_address(host):
                raise ValueError
        return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or '/', parts.query, ''))
    except (ValueError, UnicodeError):
        raise UserError(FETCH_ERROR) from None


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM,
            )
        except OSError:
            raise OSError('Public DNS resolution failed') from None
        if not addresses or any(not is_public_address(row[4][0]) for row in addresses):
            raise OSError('Non-public DNS result rejected')
        # Return only the already checked numeric addresses to aiohttp.
        return [dict(hostname=host, host=row[4][0], port=port, family=row[0],
                     proto=row[2], flags=socket.AI_NUMERICHOST) for row in addresses]

    async def close(self):
        pass


async def read_bounded_body(response) -> bytes:
    """Bound both the transferred and decoded body without unbounded flush()."""
    encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
    if encoding not in ('identity', 'gzip', 'deflate'):
        raise UserError(FETCH_ERROR)
    if response.content_length is not None and response.content_length > MAX_BYTES:
        raise UserError(FETCH_ERROR)
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == 'gzip' else None
    pending = b''
    transferred = 0
    body = bytearray()
    try:
        async for chunk in response.content.iter_chunked(32_768):
            transferred += len(chunk)
            if transferred > MAX_BYTES:
                raise UserError(FETCH_ERROR)
            if encoding == 'deflate' and decoder is None:
                pending += chunk
                if len(pending) < 2:
                    continue
                # RFC deflate has a zlib header; support the raw variant used
                # by some servers without retrying an unbounded decode.
                wrapped = pending[0] & 15 == 8 and int.from_bytes(pending[:2], 'big') % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                chunk, pending = pending, b''
            if decoder is not None:
                # max_length is a hard output bound. The extra byte lets us
                # detect overflow; flush(length) would NOT provide this bound.
                chunk = decoder.decompress(chunk, MAX_BYTES - len(body) + 1)
                if decoder.unconsumed_tail or decoder.unused_data:
                    raise UserError(FETCH_ERROR)
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise UserError(FETCH_ERROR)
        if encoding != 'identity' and (decoder is None or not decoder.eof):
            raise UserError(FETCH_ERROR)
    except zlib.error:
        raise UserError(FETCH_ERROR) from None
    return bytes(body)


class ContentFetcher:
    def __init__(self):
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False, limit=8),
                timeout=aiohttp.ClientTimeout(total=15), trust_env=False, auto_decompress=False,
                headers={'User-Agent': 'NewsWatchMVP/1.0', 'Accept-Encoding': 'gzip, deflate, identity',
                         'Accept': 'text/html,application/xhtml+xml,text/plain'},
            )
        return self._session

    async def fetch(self, url: str) -> FetchedContent:
        current = validate_url(url)
        try:
            async with asyncio.timeout(15):
                session = await self._get_session()
                for redirect in range(4):
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get('Location')
                            if not location or redirect == 3:
                                raise UserError(FETCH_ERROR)
                            current = validate_url(urljoin(current, location))
                            continue
                        if response.status != 200:
                            raise UserError(FETCH_ERROR)
                        content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
                        if content_type not in ('text/html', 'application/xhtml+xml', 'text/plain'):
                            raise UserError(FETCH_ERROR)
                        body = await read_bounded_body(response)
                        return extract_content(current, body, content_type, response.charset)
        except UserError:
            raise
        except (aiohttp.ClientError, TimeoutError, OSError, UnicodeError, ValueError):
            raise UserError(FETCH_ERROR) from None
        raise UserError(FETCH_ERROR)

    async def close(self):
        if self._session is not None:
            await self._session.close()


def extract_content(url: str, body: bytes, content_type: str, charset: str | None = None) -> FetchedContent:
    if content_type == 'text/plain':
        try:
            text = body.decode(charset or 'utf-8', errors='replace')
        except LookupError:
            text = body.decode('utf-8', errors='replace')
        title = text.splitlines()[0][:200] if text else ''
    else:
        soup = BeautifulSoup(body, 'html.parser', from_encoding=charset)
        title_node = soup.find('title')
        title = title_node.get_text(' ', strip=True)[:200] if title_node else ''
        for node in soup.find_all(['script', 'style', 'noscript', 'nav', 'aside', 'footer', 'header', 'form', 'svg']):
            node.decompose()
        for node in soup.select('[hidden], [aria-hidden="true"]'):
            node.decompose()
        main = soup.find('article') or soup.find('main') or soup.body or soup
        text = main.get_text(' ', strip=True)
    text = ' '.join(text.split())[:18_000]
    lowered = text.lower()
    if (title.lower().strip() in {'access denied', 'just a moment...', 'just a moment', 'attention required! | cloudflare'}
            or (len(text) < 2000 and any(marker in lowered for marker in (
                'enable javascript and cookies to continue', 'verify you are human',
                'checking your browser before accessing', 'please enable javascript to view this page')))):
        raise UserError(FETCH_ERROR)
    if len(text) < 100:
        raise UserError(FETCH_ERROR)
    return FetchedContent(url=url, title=title, text=text)
