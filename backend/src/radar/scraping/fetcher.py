"""A polite HTTP client: robots.txt, per-host throttling, conditional GETs, backoff.

Every request the system makes to an organisation's website goes through `Fetcher.get`, so the
etiquette rules live in exactly one place.
"""

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from radar.settings import Settings

MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_S = 60.0
DEFAULT_RETRY_AFTER_S = 10.0


class Blocked(Exception):
    """The site is refusing us (403/429 or robots.txt unreachable). Stop; don't circumvent."""


class DisallowedByRobots(Exception):
    """robots.txt disallows this URL for our user agent."""


class PrivateAddress(Blocked):
    """The URL points at this machine or a private network (localhost, 10.x, cloud metadata...).

    A website can list any URL in robots.txt, a sitemap or a redirect, and the admin's
    "Set up with AI" may be handed a wrong address. Without this check the server could be
    made to fetch its own internal services and show the answer back.
    """


class CrawlerNotIdentified(Exception):
    """SCRAPER_USER_AGENT has no real contact address, so we refuse to touch any website."""


# Text that means the user agent is still the shipped placeholder, not the operator's own.
_PLACEHOLDERS = ("example.com", "change-me", "set scraper_user_agent")


def check_user_agent(user_agent: str) -> None:
    """Refuse to crawl until the operator has put their own contact in the user agent.

    Anyone can run a copy of this project. If every copy crawled under the shipped default,
    the organisations would see traffic they can't trace or complain about. Requiring a real contact
    makes each copy's traffic its operator's responsibility, and stops someone who only wants
    to look at the demo from crawling by accident.
    """
    lowered = user_agent.lower()
    has_contact = "@" in lowered or "http://" in lowered or "https://" in lowered
    if not has_contact or any(text in lowered for text in _PLACEHOLDERS):
        raise CrawlerNotIdentified(
            "Crawling is off: set SCRAPER_USER_AGENT in .env to include your own contact, e.g. "
            '"PartnerRadarBot/0.1 (+mailto:you@yourdomain.com)". '
            "Only do this if you mean to contact the organisations' websites; the demo data needs no crawling."
        )


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content: bytes
    etag: str | None = None
    last_modified: str | None = None

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def redirected(self) -> bool:
        return self.final_url.rstrip("/") != self.url.rstrip("/")


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None = everything allowed (no robots.txt)
    fetched_at: datetime
    blocked_reason: str | None = None


@dataclass
class _HostState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        min_delay_s: float = 3.0,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        robots_ttl: timedelta = timedelta(days=1),
        allow_private: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.min_delay_s = min_delay_s
        self.max_retry_after_s = MAX_RETRY_AFTER_S
        self.robots_ttl = robots_ttl
        self.requests_made = 0
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent, "Accept-Language": "en"},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
            # Request hooks also run for every redirect, so a public URL can't bounce us inward.
            event_hooks={"request": [] if allow_private else [_refuse_private_address]},
        )
        self._robots: dict[str, _Robots] = {}
        self._hosts: dict[str, _HostState] = {}
        self._host_delay: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "Fetcher":
        """The only way the app (worker, CLI, admin "Test") builds a real fetcher."""
        check_user_agent(settings.scraper_user_agent)
        return cls(
            settings.scraper_user_agent,
            settings.scraper_min_delay_s,
            settings.scraper_timeout_s,
            allow_private=settings.scraper_allow_private_hosts,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_host_delay(self, url: str, delay_s: float) -> None:
        self._host_delay[urlsplit(url).netloc] = delay_s

    async def robots(self, url: str) -> tuple[RobotFileParser | None, str | None]:
        """The parsed robots.txt for this URL's site (None if it has none) and any block reason."""
        entry = await self._robots_for(url)
        return entry.parser, entry.blocked_reason

    async def get(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        robots = await self._robots_for(url)
        if robots.blocked_reason:
            raise Blocked(robots.blocked_reason)
        if robots.parser and not robots.parser.can_fetch(self.user_agent, url):
            raise DisallowedByRobots(url)

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = await self._throttled_get(url, headers, robots)
            if response.status_code in (429, 503):
                wait = _retry_after_seconds(response)
                if attempt < MAX_ATTEMPTS and wait <= self.max_retry_after_s:
                    await asyncio.sleep(wait)
                    continue
                raise Blocked(f"HTTP {response.status_code} from {urlsplit(url).netloc}")
            if response.status_code == 403:
                raise Blocked(f"HTTP 403 from {urlsplit(url).netloc}")
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status=response.status_code,
                content=response.content,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
        raise AssertionError("unreachable")

    async def _throttled_get(self, url: str, headers: dict[str, str], robots: _Robots) -> httpx.Response:
        host = urlsplit(url).netloc
        state = self._hosts.setdefault(host, _HostState())
        delay = self._host_delay.get(host, self.min_delay_s)
        if robots.parser:
            crawl_delay = robots.parser.crawl_delay(self.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        async with state.lock:
            wait = state.last_request + delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                self.requests_made += 1
                return await self._client.get(url, headers=headers)
            finally:
                state.last_request = time.monotonic()

    async def _robots_for(self, url: str) -> _Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        now = datetime.now(UTC)
        if cached and now - cached.fetched_at < self.robots_ttl:
            return cached

        robots_url = f"{origin}/robots.txt"
        placeholder = _Robots(parser=None, fetched_at=now)
        try:
            response = await self._throttled_get(robots_url, {}, placeholder)
        except PrivateAddress as exc:
            entry = _Robots(parser=None, fetched_at=now, blocked_reason=str(exc))
        except httpx.HTTPError as exc:
            entry = _Robots(parser=None, fetched_at=now, blocked_reason=f"robots.txt unreachable: {exc!r}")
        else:
            entry = _parse_robots(response, now)
        self._robots[origin] = entry
        return entry


async def _refuse_private_address(request: httpx.Request) -> None:
    """Raise PrivateAddress unless every address the host resolves to is on the public internet.

    If the name doesn't resolve we let the request go on: it will fail to connect anyway, with
    httpx's usual error. (A DNS answer that changes between this check and the connection could
    still slip through; this stops the common cases, not a determined attacker who owns a DNS zone.)
    """
    host = request.url.host
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]
    if any(not address.is_global for address in addresses):
        raise PrivateAddress(f"{host} is a private or local address; only public websites are fetched")


def _parse_robots(response: httpx.Response, now: datetime) -> _Robots:
    # RFC 9309: 4xx means "no restrictions"; 5xx (and 429) means "assume full disallow".
    status = response.status_code
    if status == 429 or status >= 500:
        return _Robots(parser=None, fetched_at=now, blocked_reason=f"robots.txt returned HTTP {status}")
    if status >= 400:
        return _Robots(parser=None, fetched_at=now)
    parser = RobotFileParser()
    parser.parse(response.text.splitlines())
    return _Robots(parser=parser, fetched_at=now)


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if not value:
        return DEFAULT_RETRY_AFTER_S
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_S
