"""Hardened outbound HTTP.

Every request in this project goes through :class:`SafeHttpClient`, which
provides the controls the specification demands in one place:

* explicit timeouts
* bounded retries with exponential backoff and ``Retry-After`` support
* ``ETag`` / ``Last-Modified`` conditional requests
* hard response-size limits (streamed, so an oversized body cannot exhaust memory)
* per-request error isolation - a failure raises, it never kills the caller loop
* SSRF protection: every host (and every redirect hop) must resolve to a public
  address, only http/https on 80/443 are allowed, and credentials in the URL are
  rejected.

External pages are untrusted data. This module deliberately does not parse or
act on page content.
"""

from __future__ import annotations

import asyncio
import ipaddress
import random
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .redact import scrub

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "AI-Radar/1.0 (+https://github.com/ai-radar/ai-radar; research agent)"
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_BLOCKED_V4 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "100::/64",
        "2001::/32",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


class UrlRejected(ValueError):
    """The URL is not safe to fetch (scheme, credentials, port or address)."""



def _safe_url(url: str) -> str:
    """Return a URL suitable for an error message.

    Request URLs can carry credentials: Telegram puts the bot token in the path
    (``/bot<id>:<token>/sendMessage``) and signed APIs put them in the query. Error
    strings are logged, printed to CI output and sometimes persisted, so masking
    happens where the message is built rather than at each sink. Userinfo is
    dropped entirely; the host, path and query remain for diagnosis.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return scrub(url)
    if not parts.scheme or not parts.netloc:
        return scrub(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    rebuilt = urlunsplit((parts.scheme, host, parts.path, parts.query, ""))
    return scrub(rebuilt)


class FetchError(RuntimeError):
    """A fetch failed. ``retryable`` tells the caller whether to try again."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        kind: str = "error",
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.kind = kind


@dataclass(slots=True)
class FetchResult:
    """Successful (or 304) response."""

    url: str
    final_url: str
    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    not_modified: bool = False
    content_type: str = ""
    elapsed_ms: int = 0

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")


@dataclass(slots=True)
class JsonResponse:
    """A JSON response from a provider API."""

    url: str
    status: int
    data: Any
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def retry_after(self) -> float | None:
        return parse_retry_after(self.headers.get("retry-after"))


@dataclass(slots=True)
class ProbeResult:
    """Result of a lightweight availability check used by verification."""

    url: str
    status: int | None
    ok: bool
    note: str = ""
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def is_blocked_ip(value: str) -> bool:
    """True when the address is not a public unicast address."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _BLOCKED_V4) or address.is_private
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return any(mapped in network for network in _BLOCKED_V4)
    return any(address in network for network in _BLOCKED_V6) or address.is_private


async def _default_resolver(host: str, port: int) -> list[str]:
    def _lookup() -> list[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return sorted({info[4][0] for info in infos})

    try:
        return await asyncio.to_thread(_lookup)
    except socket.gaierror as exc:  # pragma: no cover - network dependent
        raise UrlRejected(f"DNS resolution failed for {host}: {exc}") from exc


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def assert_safe_url(
    url: str,
    *,
    allowed_ports: Iterable[int] = (80, 443),
    resolver: Resolver | None = None,
    validate_dns: bool = True,
) -> str:
    """Validate a URL for outbound fetching, raising :class:`UrlRejected`."""
    if not url or len(url) > 2048:
        raise UrlRejected("URL is empty or too long")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlRejected(f"malformed URL: {exc}") from exc

    if parts.scheme not in {"http", "https"}:
        raise UrlRejected(f"scheme not allowed: {parts.scheme!r}")
    if parts.username or parts.password:
        raise UrlRejected("credentials in URL are not allowed")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise UrlRejected("URL has no host")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise UrlRejected("loopback host not allowed")

    port = parts.port or (443 if parts.scheme == "https" else 80)
    allowed = set(allowed_ports)
    if port not in allowed:
        raise UrlRejected(f"port {port} not allowed")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if is_blocked_ip(str(literal)):
            raise UrlRejected(f"address {literal} is not public")
        return url

    if validate_dns:
        addresses = await (resolver or _default_resolver)(host, port)
        if not addresses:
            raise UrlRejected(f"no addresses for {host}")
        blocked = [addr for addr in addresses if is_blocked_ip(addr)]
        if blocked:
            raise UrlRejected(f"host {host} resolves to a non-public address ({blocked[0]})")
    return url


def parse_retry_after(value: str | None) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


class SafeHttpClient:
    """Async HTTP client implementing the full retry / limit policy."""

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = 3,
        backoff_base: float = 0.5,
        max_backoff: float = 8.0,
        max_redirects: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        allowed_ports: Iterable[int] = (80, 443),
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        validate_dns: bool = True,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.max_redirects = max_redirects
        self.allowed_ports = tuple(allowed_ports)
        self.resolver = resolver
        self.validate_dns = validate_dns
        self._sleep = sleep or asyncio.sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            },
        )

    async def __aenter__(self) -> "SafeHttpClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        """GET with retries. Raises :class:`FetchError` on permanent failure."""
        conditional: dict[str, str] = dict(headers or {})
        if etag:
            conditional["If-None-Match"] = etag
        if last_modified:
            conditional["If-Modified-Since"] = last_modified

        limit = max_bytes or self.max_bytes
        delay = self.backoff_base
        last_error: FetchError | None = None
        for attempt in range(1, self.retries + 1):
            try:
                return await self._get_once(url, conditional, limit)
            except FetchError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.retries:
                    raise
                wait = exc.retry_after if exc.retry_after is not None else delay
                wait = min(max(wait, 0.0), self.max_backoff)
                await self._sleep(wait * (0.8 + random.random() * 0.4))
                delay = min(delay * 2, self.max_backoff)
        raise last_error or FetchError("request failed", kind="unknown")

    async def _get_once(self, url: str, headers: dict[str, str], max_bytes: int | None = None) -> FetchResult:
        current = url
        started = datetime.now(timezone.utc)
        for hop in range(self.max_redirects + 1):
            await assert_safe_url(
                current,
                allowed_ports=self.allowed_ports,
                resolver=self.resolver,
                validate_dns=self.validate_dns,
            )
            try:
                async with self._client.stream("GET", current, headers=headers) as response:
                    status = response.status_code
                    response_headers = {k.lower(): v for k, v in response.headers.items()}

                    if status in REDIRECT_STATUSES:
                        location = response_headers.get("location")
                        if not location:
                            raise FetchError(
                                f"redirect without Location from {_safe_url(current)}",
                                status=status,
                                kind="redirect",
                            )
                        current = urljoin(current, location)
                        continue

                    if status == 304:
                        return FetchResult(
                            url=url,
                            final_url=str(response.url),
                            status=status,
                            not_modified=True,
                            headers=response_headers,
                            elapsed_ms=self._elapsed_ms(started),
                        )

                    if status == 429:
                        raise FetchError(
                            f"rate limited by {_safe_url(current)}",
                            status=status,
                            retryable=True,
                            retry_after=parse_retry_after(response_headers.get("retry-after")),
                            kind="rate_limit",
                        )
                    if status in (408,) or 500 <= status < 600:
                        raise FetchError(
                            f"upstream error {status} from {_safe_url(current)}",
                            status=status,
                            retryable=True,
                            retry_after=parse_retry_after(response_headers.get("retry-after")),
                            kind="upstream",
                        )
                    if status >= 400:
                        raise FetchError(
                            f"HTTP {status} from {_safe_url(current)}", status=status, kind="http"
                        )

                    declared = response_headers.get("content-length")
                    limit = max_bytes or self.max_bytes
                    if declared and declared.isdigit() and int(declared) > limit:
                        raise FetchError(
                            f"declared body {declared} bytes exceeds limit",
                            status=status,
                            kind="too_large",
                        )

                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise FetchError(
                                f"response body exceeded {limit} bytes",
                                status=status,
                                kind="too_large",
                            )

                    encoding = response.encoding or "utf-8"
                    text = bytes(buffer).decode(encoding, errors="replace")
                    return FetchResult(
                        url=url,
                        final_url=str(response.url),
                        status=status,
                        text=text,
                        headers=response_headers,
                        content_type=response_headers.get("content-type", ""),
                        elapsed_ms=self._elapsed_ms(started),
                    )
            except httpx.TimeoutException as exc:
                raise FetchError(
                    f"timeout after {self.timeout}s for {_safe_url(current)}", retryable=True, kind="timeout"
                ) from exc
            except httpx.HTTPError as exc:
                raise FetchError(
                    f"transport error for {_safe_url(current)}: {type(exc).__name__}", retryable=True, kind="transport"
                ) from exc

        raise FetchError(f"too many redirects for {_safe_url(url)}", kind="redirect")

    async def post_json(
        self,
        url: str,
        *,
        payload: Any = None,
        headers: dict[str, str] | None = None,
    ) -> JsonResponse:
        """POST a JSON body and decode a JSON response.

        No automatic retries: the intelligence layer owns provider fallback, and
        blindly retrying a paid-per-token endpoint is exactly what must not happen.
        """
        import json

        await assert_safe_url(
            url,
            allowed_ports=self.allowed_ports,
            resolver=self.resolver,
            validate_dns=self.validate_dns,
        )
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        try:
            async with self._client.stream(
                "POST", url, headers=request_headers, json=payload
            ) as response:
                status = response.status_code
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > self.max_bytes:
                        raise FetchError(
                            f"response body exceeded {self.max_bytes} bytes",
                            status=status,
                            kind="too_large",
                        )
                text = bytes(buffer).decode("utf-8", errors="replace")
                data: Any = None
                if text.strip():
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        data = None
                return JsonResponse(
                    url=url,
                    status=status,
                    data=data,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    text=text,
                )
        except httpx.TimeoutException as exc:
            raise FetchError(
                f"timeout after {self.timeout}s for {_safe_url(url)}", retryable=True, kind="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FetchError(
                f"transport error for {_safe_url(url)}: {type(exc).__name__}", retryable=True, kind="transport"
            ) from exc

    async def send_raw(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> tuple[int, bytes, dict[str, str]]:
        """Send a request with a pre-built body (multipart uploads, DELETE).

        The single escape hatch from the JSON helpers, kept narrow on purpose: it
        still validates the URL, caps the response and never retries.
        """
        import httpx

        await assert_safe_url(
            url,
            allowed_ports=self.allowed_ports,
            resolver=self.resolver,
            validate_dns=self.validate_dns,
        )
        limit = max_bytes or self.max_bytes
        try:
            async with self._client.stream(
                method,
                url,
                content=content,
                headers=headers,
                timeout=httpx.Timeout(timeout or self.timeout, connect=min(20.0, timeout or self.timeout)),
            ) as response:
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        raise FetchError(
                            f"response body exceeded {limit} bytes",
                            status=response.status_code,
                            kind="too_large",
                        )
                return (
                    response.status_code,
                    bytes(buffer),
                    {k.lower(): v for k, v in response.headers.items()},
                )
        except httpx.TimeoutException as exc:
            raise FetchError(
                f"timeout after {timeout or self.timeout}s for {_safe_url(url)}", retryable=True, kind="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise FetchError(
                f"transport error for {_safe_url(url)}: {type(exc).__name__}", retryable=True, kind="transport"
            ) from exc

    async def probe(self, url: str) -> ProbeResult:
        """Check availability without retries. Never raises."""
        try:
            await assert_safe_url(
                url,
                allowed_ports=self.allowed_ports,
                resolver=self.resolver,
                validate_dns=self.validate_dns,
            )
        except UrlRejected as exc:
            return ProbeResult(url=url, status=None, ok=False, note=f"rejected: {exc}")
        last = "unreachable"
        for method in ("HEAD", "GET"):
            try:
                async with self._client.stream(method, url, headers={"Accept": "*/*"}) as response:
                    status = response.status_code
                    if method == "HEAD" and status in (403, 405, 501):
                        continue
                    ok = 200 <= status < 400 or status in (401, 403)  # exists but gated
                    return ProbeResult(
                        url=url,
                        status=status,
                        ok=ok,
                        note="" if ok else f"HTTP {status}",
                    )
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
        return ProbeResult(url=url, status=None, ok=False, note=last)

    @staticmethod
    def _elapsed_ms(started: datetime) -> int:
        return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


async def fetch_json_document(
    client: SafeHttpClient, url: str, **kwargs: Any
) -> tuple[Any, FetchResult]:
    """GET and decode JSON, returning the payload **and** the raw result.

    The result is returned so a collector can persist the ``ETag`` /
    ``Last-Modified`` validators from the response; without them a conditional
    request can never be made and every run pays for the full body.

    A ``304 Not Modified`` is raised as ``kind="not_modified"`` rather than being
    parsed as an empty body, which would otherwise look like invalid JSON and be
    recorded as a source failure.
    """
    import json

    result = await client.get(url, **kwargs)
    if result.not_modified:
        raise FetchError(
            f"not modified: {_safe_url(url)}", status=304, kind="not_modified"
        )
    try:
        return json.loads(result.text), result
    except json.JSONDecodeError as exc:
        raise FetchError(f"invalid JSON from {_safe_url(url)}: {exc.msg}", kind="invalid_json") from exc


async def fetch_json(client: SafeHttpClient, url: str, **kwargs: Any) -> Any:
    """GET and decode JSON, raising :class:`FetchError` on a bad payload."""
    payload, _ = await fetch_json_document(client, url, **kwargs)
    return payload
