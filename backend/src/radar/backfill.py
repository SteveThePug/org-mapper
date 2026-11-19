"""Cold start: reconstruct recent market history from the Internet Archive.

A new deployment has no history: the first crawl is a baseline, so the moves feed starts empty
and stays thin for weeks. The Wayback Machine keeps dated copies of organisations' sitemaps and bio
pages. Reading an organisation's sitemap as it was N months ago and comparing it with today's crawl -
using the same extraction and diff code as the live pipeline - yields *real* joins, departures,
promotions and lateral moves over that window.

Everything produced here is marked `source="archive"` and states its time window.
"""

import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from radar.models import Person, person_id_for
from radar.pipeline.diff import EventDraft
from radar.pipeline.normalise import apply_record, normalise_title
from radar.scraping.extract import extract_profile
from radar.scraping.fetcher import Blocked, DisallowedByRobots
from radar.scraping.sources import discover
from radar.worker.crawler import Crawler

log = logging.getLogger(__name__)

WAYBACK = "https://web.archive.org"
ARCHIVE_DELAY_S = 4.0  # the Wayback Machine throttles bursts; ~15 pages/minute is reliable
MAX_CONSECUTIVE_FAILURES = 5
# If more than this share of URLs differ, the site was probably restructured - not real churn.
MAX_CHANGE_SHARE = 0.25
MIN_CHANGE_TO_DOUBT = 20  # small organisations can legitimately change by a large share
# An archived bio is only trusted if captured within this distance of the sitemap snapshot.
MAX_CAPTURE_DRIFT = timedelta(days=120)
_TIMESTAMP = re.compile(r"/web/(\d{14})")


def archived(url: str, timestamp: str) -> str:
    """Raw archived copy (`id_`) of `url` nearest to `timestamp`; Wayback redirects to the closest capture."""
    return f"{WAYBACK}/web/{timestamp}id_/{url}"


def _too_different(changed: int, total: int) -> bool:
    return changed > max(MIN_CHANGE_TO_DOUBT, MAX_CHANGE_SHARE * total)


def capture_time(url: str) -> datetime | None:
    """When the Wayback Machine captured this archive URL (`.../web/20250131120000/...`)."""
    match = _TIMESTAMP.search(url)
    return parse_timestamp(match.group(1)) if match else None


def parse_timestamp(timestamp: str) -> datetime:
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


class ArchiveUnavailable(Exception):
    """The archive keeps refusing (503/429): stop and let a later run continue."""


class ArchiveFetchFailed(Exception):
    """One archived page couldn't be fetched this time; try it again on a later run."""


class Backfiller:
    def __init__(self, crawler: Crawler) -> None:
        self.consecutive_failures = 0
        self.crawler = crawler
        self.db = crawler.db
        self.fetcher = crawler.fetcher
        self.config = crawler.config
        self.fetcher.set_host_delay(WAYBACK, ARCHIVE_DELAY_S)
        self.fetcher.max_retry_after_s = 15  # a throttled archive: give up quickly, resume on a later run

    async def run(
        self, org_id: str, months: int = 6, max_pages: int = 300, force: bool = False
    ) -> dict[str, Any]:
        organisation = self.config.organisation(org_id)
        org = await self.db["organisations"].find_one({"_id": organisation.id}) or {}
        if organisation.discovery is None:
            return {"skipped": "organisation not configured"}
        if org.get("backfilled_at") and not force:
            return {"skipped": f"already backfilled at {org['backfilled_at']:%Y-%m-%d} (use --force)"}

        now = datetime.now(UTC)
        progress = org.get("backfill") or {}
        if progress.get("remaining") and progress.get("snapshot_id") and not force:
            snapshot = progress["snapshot_id"]  # resume the unfinished backfill against the same snapshot
        else:
            snapshot = await self._closest_capture(
                organisation.discovery.urls[0], now - timedelta(days=30 * months)
            )
        if snapshot is None:
            return {"skipped": "no archived copy of the sitemap"}
        since = parse_timestamp(snapshot)
        old_urls = {
            e.url
            for e in await discover(self.fetcher, organisation.discovery, via=lambda u: archived(u, snapshot))
        }

        current: dict[str, Person] = {}
        async for doc in self.db["people"].find(
            {"org_id": organisation.id, "status": {"$in": ["pending", "active"]}}
        ):
            current[doc["profile_url"]] = Person.model_validate(doc)
        known_departed = {
            doc["profile_url"]
            async for doc in self.db["people"].find({"org_id": organisation.id, "status": "departed"})
        }
        suppressed = {doc["_id"] async for doc in self.db["suppressions"].find({}, {"_id": 1})}
        joined = current.keys() - old_urls
        departed = {
            u for u in old_urls - current.keys() - known_departed if person_id_for(u) not in suppressed
        }
        counts: dict[str, Any] = {
            "snapshot": f"{since:%Y-%m-%d}",
            "snapshot_id": snapshot,
            "urls_then": len(old_urls),
            "urls_now": len(current),
            "joined_urls": len(joined),
            "departed_urls": len(departed),
        }
        if (
            not old_urls
            or _too_different(len(joined), len(current))
            or _too_different(len(departed), len(old_urls))
        ):
            return counts | {"skipped": "archived and current URL lists differ too much (site restructured?)"}

        window = f"between {since:%d %b %Y} and {now:%d %b %Y} (Internet Archive)"
        counts["joined"] = await self._joins(joined, current, organisation.name, window)
        # Pages an earlier run already read against this snapshot (it was cut short by the page
        # budget or an archive outage) are not read again, so repeated runs finish the whole list.
        checked = await self._checked(organisation.id, snapshot, "departure")
        to_read = {u for u in departed if person_id_for(u) not in checked}
        aborted: str | None = None
        used_left = used_promoted = 0
        try:
            counts["left"], used_left = await self._departures(
                to_read, organisation.id, organisation.name, snapshot, since, window, max_pages
            )
            counts["promoted"], used_promoted = await self._promotions(
                current,
                old_urls,
                organisation.id,
                organisation.name,
                snapshot,
                since,
                window,
                max_pages - used_left,
            )
        except ArchiveUnavailable as exc:
            aborted = str(exc)  # everything is idempotent: a later run resumes without duplicates
        checked = await self._checked(organisation.id, snapshot, "departure")
        counts["archived_pages_read"] = used_left + used_promoted
        counts["remaining"] = remaining = sum(person_id_for(u) not in checked for u in to_read) + len(
            await self._promotion_candidates(current, old_urls, organisation.id, snapshot)
        )
        fields: dict[str, Any] = {"backfill": counts}
        if remaining == 0:
            fields["backfilled_at"] = now  # only a finished backfill counts as done
        await self.db["organisations"].update_one({"_id": organisation.id}, {"$set": fields})
        return counts | ({"aborted": aborted} if aborted else {})

    async def _joins(self, urls: set[str], current: dict[str, Person], org_name: str, window: str) -> int:
        """On the site now, absent from the archived sitemap: arrived during the window.

        Out-of-scope arrivals get no "joined" event, but they are no longer baseline and are matched
        against departures: a partner who left to take another title elsewhere is still a move.
        """
        emitted = 0
        for url in urls:
            person = current[url]
            if person.status != "active" or not person.baseline:
                continue  # not read yet, or already reported by a live crawl
            await self.db["people"].update_one({"_id": person.id}, {"$set": {"baseline": False}})
            person.baseline = False
            if not person.in_scope:
                await self.crawler.match_moves(person, joined=True, source="archive")
                continue
            offices = f" ({', '.join(person.offices)})" if person.offices else ""
            await self.crawler.emit(
                person,
                # "appeared", not "joined": it may be a lateral hire or a promotion to a listed title.
                EventDraft(
                    "joined", f"{person.name} appeared at {org_name}{offices} as {person.title} {window}"
                ),
                source="archive",
            )
            await self.crawler.match_moves(person, joined=True, source="archive")
            emitted += 1
        return emitted

    async def _departures(
        self,
        urls: set[str],
        org_id: str,
        org_name: str,
        snapshot: str,
        since: datetime,
        window: str,
        budget: int,
    ) -> tuple[int, int]:
        """In the archived sitemap, gone now: read their archived bio to see who they were."""
        emitted = used = 0
        for url in sorted(urls):
            if used >= budget:
                break
            used += 1
            if used % 25 == 0:
                log.info(
                    "%s: %d/%d archived departures checked, %d in segment", org_id, used, len(urls), emitted
                )
            try:
                page = await self._archived_page(url, snapshot, since)
            except ArchiveFetchFailed:
                continue  # not marked as checked: a later run tries it again
            await self._mark_checked(org_id, snapshot, "departure", person_id_for(url))
            if page is None:
                continue
            html, archive_url = page
            record = extract_profile(
                html, url, self.config.organisation(org_id).selectors, self.config.domain.fields
            )
            if not record.name:
                continue
            stub = Person(
                id=person_id_for(url), org_id=org_id, profile_url=url, baseline=False, first_seen=since
            )
            person = apply_record(stub, record, self.config.domain, since)
            if not person.in_scope:
                continue
            now = datetime.now(UTC)
            person.status, person.departed_at, person.last_fetched = "departed", now, None
            await self.db["people"].replace_one({"_id": person.id}, person.to_mongo(), upsert=True)
            await self.crawler.emit(
                person,
                EventDraft(
                    "left",
                    f"{person.name} left {org_name} ({', '.join(person.offices)}) - profile removed {window}",
                    before={
                        "title": person.title,
                        "offices": person.offices,
                        "groups": person.groups_raw,
                    },
                ),
                evidence_url=archive_url,
                source="archive",
            )
            await self.crawler.match_moves(person, joined=False, source="archive")
            emitted += 1
        return emitted, used

    async def _promotions(
        self,
        current: dict[str, Person],
        old_urls: set[str],
        org_id: str,
        org_name: str,
        snapshot: str,
        since: datetime,
        window: str,
        budget: int,
    ) -> tuple[int, int]:
        """In-scope people today in a `promotion_to` title whose archived page shows another title."""
        emitted = used = 0
        titles = self.config.domain.titles
        for person in await self._promotion_candidates(current, old_urls, org_id, snapshot):
            if used >= budget:
                break
            used += 1
            try:
                page = await self._archived_page(person.profile_url, snapshot, since)
            except ArchiveFetchFailed:
                continue
            await self._mark_checked(org_id, snapshot, "promotion", person.id)
            if page is None:
                continue
            html, _ = page
            selectors = self.config.organisation(person.org_id).selectors
            record = extract_profile(html, person.profile_url, selectors, self.config.domain.fields)
            old_title = normalise_title(record.title, titles)
            # Same rule as live diffs: a change into a `promotion_to` title from another known one.
            if old_title not in (None, "other", *self.config.domain.promotion_to):
                await self.crawler.emit(
                    person,
                    EventDraft(
                        "promoted",
                        f"{person.name} was promoted from {record.title} to {person.title} at {org_name} {window}",
                        before={"title": record.title},
                        after={"title": person.title},
                    ),
                    source="archive",
                )
                emitted += 1
        return emitted, used

    async def _promotion_candidates(
        self, current: dict[str, Person], old_urls: set[str], org_id: str, snapshot: str
    ) -> list[Person]:
        # People already given an archive promotion, or whose archived page an earlier run already
        # read, are skipped: re-running never duplicates events and always makes progress.
        already = set(
            await self.db["events"].distinct("person_id", {"type": "promoted", "source": "archive"})
        ) | await self._checked(org_id, snapshot, "promotion")
        return [
            p
            for url, p in current.items()
            if url in old_urls
            and p.in_scope
            and p.title_norm in self.config.domain.promotion_to
            and p.id not in already
        ]

    async def _checked(self, org_id: str, snapshot: str, kind: str) -> set[str]:
        """Person ids whose archived page was already read for this snapshot."""
        query = {"org_id": org_id, "snapshot": snapshot, "kind": kind}
        return set(await self.db["archive_checked"].distinct("person_id", query))

    async def _mark_checked(self, org_id: str, snapshot: str, kind: str, person_id: str) -> None:
        # Only ids (hashes of profile URLs) are kept here - no personal data.
        await self.db["archive_checked"].update_one(
            {"_id": f"{org_id}:{snapshot}:{kind}:{person_id}"},
            {"$set": {"org_id": org_id, "snapshot": snapshot, "kind": kind, "person_id": person_id}},
            upsert=True,
        )

    async def _archived_page(self, url: str, snapshot: str, since: datetime) -> tuple[str, str] | None:
        """The archived HTML and a human-viewable archive link, or None if no usable capture exists.

        Raises ArchiveFetchFailed if the request itself failed (worth retrying later).
        """
        try:
            result = await self.fetcher.get(archived(url, snapshot))
        except (httpx.HTTPError, Blocked, DisallowedByRobots) as exc:
            log.warning("archive fetch failed for %s: %s", url, exc)
            self.consecutive_failures += 1
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise ArchiveUnavailable(
                    f"{self.consecutive_failures} archive requests failed in a row: {exc}"
                ) from exc
            raise ArchiveFetchFailed(str(exc)) from exc
        self.consecutive_failures = 0
        captured = capture_time(result.final_url)
        if result.status != 200 or captured is None or abs(captured - since) > MAX_CAPTURE_DRIFT:
            return None
        # Without `id_` the Wayback Machine shows its own banner and toolbar: the right link for a human.
        return result.text, result.final_url.replace("id_/", "/", 1)

    async def _closest_capture(self, url: str, target: datetime) -> str | None:
        query = (
            f"{WAYBACK}/cdx/search/cdx?url={quote(url, safe='')}&output=json&filter=statuscode:200"
            f"&closest={target:%Y%m%d}&sort=closest&limit=1"
        )
        try:
            result = await self.fetcher.get(query)
        except (httpx.HTTPError, Blocked) as exc:
            log.warning("CDX lookup failed for %s: %s", url, exc)
            return None
        try:
            rows = json.loads(result.content) if result.status == 200 else []
        except ValueError:
            return None
        # CDX JSON is a header row followed by result rows; column 1 is the capture timestamp.
        return str(rows[1][1]) if len(rows) > 1 else None
