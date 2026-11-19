"""The scraping pipeline: discover -> fetch -> extract -> normalise -> diff -> events.

`discover_org` and `fetch_profile` are the two units of work the worker runs; the CLI calls
the same methods for one-off runs, so there is a single code path.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx
from pymongo import UpdateOne

from radar.config import AppConfig, OrgConfig
from radar.db import Database
from radar.domain import refresh_domain, seed_domain
from radar.llm.classify import LabelClassifier
from radar.models import Event, Observation, Person, ProfileRecord, RoleSnapshot, person_id_for
from radar.orgs import refresh_orgs, seed_orgs
from radar.pipeline.diff import EventDraft, diff_person, left_event
from radar.pipeline.health import assess_discovery
from radar.pipeline.matching import MOVE_THRESHOLD, POSSIBLE_THRESHOLD, match_score
from radar.pipeline.normalise import apply_record
from radar.scraping.extract import PARSER_VERSION, extract_profile
from radar.scraping.fetcher import Blocked, DisallowedByRobots, Fetcher
from radar.scraping.raw_store import RawStore
from radar.scraping.sitemap import SitemapEntry
from radar.scraping.sources import discover
from radar.settings import Settings
from radar.worker import jobs

log = logging.getLogger(__name__)

MOVE_WINDOW = timedelta(days=120)
LIFT_OUT_WINDOW = timedelta(days=60)
MISSING_RUNS_BEFORE_VERIFY = 2
# Written only by `link_move`, never by a profile fetch.
MOVE_LINK_FIELDS = {"_id", "history", "moved_from", "moved_to"}
# A smaller profile list seen this many runs in a row is accepted as the new normal.
QUARANTINE_RUNS_BEFORE_ACCEPT = 3
# How long to wait before re-checking a profile page that suddenly returned 404 or redirected away.
VERIFY_DELAY = timedelta(hours=12)
# Appended to an unconfirmed move's summary; stripped again when a recruiter confirms it.
POSSIBLE_MATCH_SUFFIX = " (possible match"


class FetchError(Exception):
    """Transient failure; the job will be retried with backoff."""


class PlannedFetch(NamedTuple):
    person_id: str
    priority: int
    reason: str  # "new", "changed", "stale" or "verify"


@dataclass
class Crawler:
    db: Database
    fetcher: Fetcher
    config: AppConfig
    settings: Settings
    raw_store: RawStore
    # AI fallback for group labels no keyword rule matches; None = keyword rules only.
    labels: LabelClassifier | None = None

    # --- discovery --------------------------------------------------------------------

    async def discover_org(self, org_id: str, limit: int | None = None) -> dict[str, Any]:
        """Diff the organisation's sitemap against what we know; queue the fetches that are needed."""
        organisation = self.config.organisation(org_id)
        now = datetime.now(UTC)
        run: dict[str, Any] = {"_id": uuid.uuid4().hex, "org_id": organisation.id, "started_at": now}
        org = await self.db["organisations"].find_one({"_id": organisation.id}) or {}

        if organisation.discovery is None or not organisation.enabled:
            await self._set_org(
                organisation.id,
                status="not_configured",
                reason=organisation.notes or "no discovery configured",
            )
            return await self._finish_run(run, health="skipped")
        if organisation.min_delay_s:
            self.fetcher.set_host_delay(organisation.website, organisation.min_delay_s)

        requests_before = self.fetcher.requests_made
        try:
            entries = await discover(self.fetcher, organisation.discovery)
        except (Blocked, DisallowedByRobots) as exc:
            await self.block_org(organisation.id, f"{type(exc).__name__}: {exc}")
            return await self._finish_run(run, health="blocked", error=str(exc))
        except (httpx.HTTPError, ValueError) as exc:
            # Still schedule the next attempt, so a failing site is retried at the normal interval
            # instead of on every scheduler tick.
            await self._set_org(
                organisation.id,
                status="degraded",
                reason=f"discovery failed: {exc!r}",
                last_discovery_at=now,
                next_discovery_at=now + timedelta(hours=self.settings.discovery_interval_h),
            )
            return await self._finish_run(run, health="error", error=repr(exc))

        await self._record_requests(organisation.id, self.fetcher.requests_made - requests_before, 0)
        health = assess_discovery(org.get("last_discovered_count"), len(entries))
        suppressed = {doc["_id"] async for doc in self.db["suppressions"].find({}, {"_id": 1})}
        entries = [entry for entry in entries if person_id_for(entry.url) not in suppressed]
        baseline = not org.get("discoveries")
        known = {
            doc["profile_url"]: doc
            async for doc in self.db["people"].find(
                {"org_id": organisation.id, "status": {"$in": ["pending", "active"]}},
                {"profile_url": 1, "sitemap_lastmod": 1, "last_fetched": 1, "segments": 1, "missing_runs": 1},
            )
        }

        to_fetch = await self._upsert_discovered(organisation.id, entries, known, baseline, now)
        seen_urls = {entry.url for entry in entries}
        missing = [doc for url, doc in known.items() if url not in seen_urls]
        to_verify: list[PlannedFetch] = []
        if health == "ok":  # a quarantined run never marks anyone as gone
            to_verify = await self._handle_missing(missing, now)

        planned = to_fetch + to_verify
        if limit is not None:
            planned = sorted(planned, key=lambda fetch: fetch.priority, reverse=True)[:limit]
        for fetch in planned:
            await jobs.enqueue(
                self.db,
                "fetch",
                f"fetch:{fetch.person_id}",
                organisation.id,
                {"person_id": fetch.person_id, "reason": fetch.reason},
                fetch.priority,
            )

        await self._record_discovery(organisation.id, health, len(entries), now)
        return await self._finish_run(
            run,
            health=health,
            baseline=baseline,
            discovered=len(entries),
            new=sum(1 for fetch in planned if fetch.reason == "new"),
            queued=len(planned),
            missing=len(missing),
            verifying=len(to_verify),
            requests=self.fetcher.requests_made - requests_before,
        )

    async def _upsert_discovered(
        self,
        org_id: str,
        entries: list[SitemapEntry],
        known: dict[str, dict[str, Any]],
        baseline: bool,
        now: datetime,
    ) -> list[PlannedFetch]:
        """Record every discovered URL (as a pending stub if new) and decide what to fetch."""
        upserts: list[UpdateOne] = []
        to_fetch: list[PlannedFetch] = []
        for entry in entries:
            pid = person_id_for(entry.url)
            stub = Person(
                id=pid, org_id=org_id, profile_url=entry.url, baseline=baseline, first_seen=now
            ).to_mongo()
            seen = {"last_seen_in_sitemap": now, "sitemap_lastmod": entry.lastmod, "missing_runs": 0}
            for key in seen:  # Mongo rejects an update that sets the same field in $set and $setOnInsert
                stub.pop(key, None)
            upserts.append(UpdateOne({"_id": pid}, {"$setOnInsert": stub, "$set": seen}, upsert=True))

            existing = known.get(entry.url)
            if existing is None or existing.get("last_fetched") is None:
                to_fetch.append(PlannedFetch(pid, jobs.PRIORITY_NEW, "new"))
            elif entry.lastmod and entry.lastmod != existing.get("sitemap_lastmod"):
                to_fetch.append(PlannedFetch(pid, jobs.PRIORITY_CHANGED, "changed"))
            elif self._is_stale(existing, now):
                in_scope = bool(existing.get("segments"))
                priority = jobs.PRIORITY_STALE_IN_SCOPE if in_scope else jobs.PRIORITY_STALE_OTHER
                to_fetch.append(PlannedFetch(pid, priority, "stale"))
        if upserts:
            await self.db["people"].bulk_write(upserts, ordered=False)
        return to_fetch

    async def _handle_missing(self, missing: list[dict[str, Any]], now: datetime) -> list[PlannedFetch]:
        """Profiles absent from the sitemap. In-scope people are verified against their bio page
        before anything is said about them; others are quietly marked as departed."""
        to_verify: list[PlannedFetch] = []
        for doc in missing:
            runs = doc.get("missing_runs", 0) + 1
            update: dict[str, Any] = {"missing_runs": runs}
            if runs >= MISSING_RUNS_BEFORE_VERIFY:
                if doc.get("segments"):
                    to_verify.append(PlannedFetch(doc["_id"], jobs.PRIORITY_VERIFY, "verify"))
                else:
                    update |= {"status": "departed", "departed_at": now}
            await self.db["people"].update_one({"_id": doc["_id"]}, {"$set": update})
        return to_verify

    async def _record_discovery(self, org_id: str, health: str, discovered: int, now: datetime) -> None:
        org_update: dict[str, Any] = {
            "last_discovery_at": now,
            "next_discovery_at": now + timedelta(hours=self.settings.discovery_interval_h),
            "last_health": health,
        }
        org = await self.db["organisations"].find_one({"_id": org_id}, {"quarantine_streak": 1}) or {}
        streak = org.get("quarantine_streak", 0) + 1 if health == "quarantined" else 0
        if health == "ok" or streak >= QUARANTINE_RUNS_BEFORE_ACCEPT:
            # A drop that holds steady for several runs is real (e.g. the site removed a section),
            # so it becomes the new baseline; otherwise the organisation stays quarantined for good.
            org_update["last_discovered_count"] = discovered
            streak = 0
        org_update["quarantine_streak"] = streak
        outcomes: dict[str, tuple[str, str | None]] = {
            "ok": ("ok", None),
            "quarantined": ("degraded", f"profile list shrank to {discovered}: departures paused"),
            "broken": ("degraded", "no profiles discovered: discovery config needs attention"),
        }
        org_status, org_reason = outcomes[health]
        update: dict[str, Any] = {"$set": org_update | {"status": org_status, "status_reason": org_reason}}
        if health != "broken":
            # Only a discovery that found profiles ends the baseline; otherwise everyone found by the
            # next (working) run would be reported as having just joined.
            update |= {"$inc": {"discoveries": 1}, "$min": {"first_discovery_at": now}}
        await self.db["organisations"].update_one({"_id": org_id}, update)

    def _is_stale(self, doc: dict[str, Any], now: datetime) -> bool:
        days = (
            self.settings.refresh_in_scope_days
            if doc.get("segments")
            else self.settings.refresh_out_of_scope_days
        )
        last: datetime | None = doc.get("last_fetched")
        return last is None or now - last > timedelta(days=days)

    async def _finish_run(self, run: dict[str, Any], **fields: Any) -> dict[str, Any]:
        run |= fields | {"finished_at": datetime.now(UTC)}
        await self.db["scrape_runs"].insert_one(run)
        log.info("discovery %s: %s", run["org_id"], {k: v for k, v in run.items() if k != "_id"})
        return run

    # --- fetching ---------------------------------------------------------------------

    async def fetch_profile(self, person_id: str, reason: str) -> str:
        doc = await self.db["people"].find_one({"_id": person_id})
        if doc is None:
            return "unknown person"
        person = Person.model_validate(doc)
        organisation = self.config.organisation(person.org_id)
        if organisation.min_delay_s:
            self.fetcher.set_host_delay(organisation.website, organisation.min_delay_s)
        conditional = reason not in ("verify", "user") and person.status == "active"
        try:
            result = await self.fetcher.get(
                person.profile_url,
                etag=person.etag if conditional else None,
                last_modified=person.last_modified if conditional else None,
            )
        except DisallowedByRobots:
            return "skipped: disallowed by robots.txt"
        except httpx.HTTPError as exc:
            raise FetchError(repr(exc)) from exc

        await self._record_requests(person.org_id, 1, len(result.content))
        now = datetime.now(UTC)
        if result.status == 304:
            await self._touch(person.id, now)
            return "not modified"
        gone = result.status in (404, 410) or (
            result.status == 200
            and result.redirected
            and organisation.discovery is not None
            and not organisation.discovery.matches(result.final_url.rstrip("/"))
        )
        if gone:
            if reason != "verify" and person.status == "active" and person.in_scope:
                # One 404 or odd redirect can be a site glitch or a URL change. Check again later
                # before telling anyone this person has left.
                await jobs.enqueue(
                    self.db,
                    "fetch",
                    f"fetch:{person.id}",
                    person.org_id,
                    {"person_id": person.id, "reason": "verify"},
                    jobs.PRIORITY_VERIFY,
                    run_after=now + VERIFY_DELAY,
                )
                return f"HTTP {result.status}: verifying again before marking departed"
            await self._mark_departed(person, organisation, now)
            return "departed"
        if result.status != 200:
            raise FetchError(f"HTTP {result.status}")

        record = extract_profile(
            result.text, person.profile_url, organisation.selectors, self.config.domain.fields
        )
        if not record.name:
            self.raw_store.put(result.content)  # keep it so a parser fix can be replayed
            await self.db["people"].update_one(
                {"_id": person.id}, {"$set": {"last_fetched": now, "extraction_error": True}}
            )
            return "extraction failed"

        record_hash = record.content_hash()
        if record_hash == person.record_hash and person.status == "active":
            await self._touch(person.id, now, etag=result.etag, last_modified=result.last_modified)
            return "unchanged"

        updated = apply_record(person, record, self.config.domain, now, await self._learned(record.groups))
        # The raw page holds the full profile (bio, education...). Keep it only for people we
        # track; for anyone else the minimal record in the observation is enough to reprocess.
        raw_sha = self.raw_store.put(result.content) if updated.in_scope else None
        updated.record_hash = record_hash
        updated.etag = result.etag
        updated.last_modified = result.last_modified
        updated.last_changed = now
        updated.departed_at = None
        # A profile that comes back after being marked departed is updated silently, without events.
        drafts = (
            []
            if person.status == "departed"
            else diff_person(person, updated, organisation.name, self.config.domain)
        )
        # Out-of-scope people: keep only what decides whether they are in scope (data minimisation).
        recorded_fields = (
            None if updated.in_scope else {"url", "name", "title", "offices", "groups", "methods"}
        )

        await self._save(updated)
        await self.db["observations"].insert_one(
            Observation(
                id=uuid.uuid4().hex,
                person_id=person.id,
                org_id=person.org_id,
                url=person.profile_url,
                fetched_at=now,
                http_status=result.status,
                record_hash=record_hash,
                raw_sha256=raw_sha,
                parser_version=PARSER_VERSION,
                record=record.model_dump(include=recorded_fields),
            ).to_mongo()
        )
        for draft in drafts:
            await self.emit(updated, draft)
        # Any profile read for the first time - newcomer or baseline, in a target segment or not
        # (a partner may move to a title we don't track) - could be someone who just left another
        # organisation we track.
        if person.status == "pending":
            await self.match_moves(updated, joined=True)
        return f"updated ({len(drafts)} events)"

    async def _save(self, person: Person) -> None:
        """Write what the crawler derived, leaving the move links alone.

        `link_move` can update `history`/`moved_*` from another lane (or a recruiter confirming a
        move) while this fetch is in flight; replacing the whole document would wipe that.
        """
        fields = {k: v for k, v in person.to_mongo().items() if k not in MOVE_LINK_FIELDS}
        await self.db["people"].update_one({"_id": person.id}, {"$set": fields})

    async def _learned(self, labels: list[str]) -> dict[str, list[str]] | None:
        """Saved (or freshly asked) AI tags for labels the keyword rules miss."""
        return await self.labels.learned_tags(labels, self.config.domain) if self.labels else None

    async def _record_requests(self, org_id: str, requests: int, nbytes: int) -> None:
        """Daily per-organisation request/bandwidth counters, for cost reporting and politeness audits."""
        day = f"{datetime.now(UTC):%Y-%m-%d}"
        await self.db["crawl_stats"].update_one(
            {"_id": f"{day}:{org_id}"},
            {"$inc": {"requests": requests, "bytes": nbytes}, "$set": {"day": day, "org_id": org_id}},
            upsert=True,
        )

    async def _touch(self, person_id: str, now: datetime, **headers: str | None) -> None:
        fields: dict[str, Any] = {"last_fetched": now, "missing_runs": 0}
        fields |= {key: value for key, value in headers.items() if value}
        await self.db["people"].update_one({"_id": person_id}, {"$set": fields})

    async def _mark_departed(self, person: Person, organisation: OrgConfig, now: datetime) -> None:
        if person.status == "departed":
            return
        await self.db["people"].update_one(
            {"_id": person.id}, {"$set": {"status": "departed", "departed_at": now, "last_fetched": now}}
        )
        if person.status == "active" and person.in_scope:
            person.status, person.departed_at = "departed", now
            await self.emit(person, left_event(person, organisation.name))
            await self.match_moves(person, joined=False)
            await self._detect_lift_out(person, organisation, now)

    # --- events -----------------------------------------------------------------------

    async def emit(self, person: Person, draft: EventDraft, **extra: Any) -> Event:
        fields: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "type": draft.type,
            "person_id": person.id,
            "person_name": person.name or "Unknown",
            "org_id": person.org_id,
            "detected_at": datetime.now(UTC),
            "summary": draft.summary,
            "before": draft.before,
            "after": draft.after,
            "segments": person.segments,
            "offices": person.offices,
            "evidence_url": person.profile_url,
        }
        event = Event(**(fields | extra))
        await self.db["events"].insert_one(event.to_mongo())
        return event

    async def match_moves(self, person: Person, joined: bool, source: str = "crawl") -> bool:
        """Pair a new arrival with a recent departure elsewhere (or vice versa).

        Called when someone leaves and on the first read of every profile (newcomer or baseline),
        so the move is found whichever side we see first. Only one side has to be in a target
        segment: a partner who leaves to become counsel elsewhere is still news.

        A *baseline* arrival (already listed when we started tracking their organisation) may have
        been there for years: we can't show they arrived recently, so the match is only suggested
        and a recruiter confirms it. If a backfill later proves they arrived recently, the
        suggestion is upgraded to a confirmed move. Returns whether a move event was written or
        upgraded.
        """
        since = datetime.now(UTC) - MOVE_WINDOW
        if joined:
            query: dict[str, Any] = {"status": "departed", "departed_at": {"$gte": since}, "moved_to": None}
        else:
            query = {"status": "active", "first_seen": {"$gte": since}, "moved_from": None}
        query["org_id"] = {"$ne": person.org_id}
        if not person.in_scope:
            query["segments"] = {"$ne": []}  # one side must be in a target segment
        earlier = await self._earlier_move_events(person, joined)
        best: tuple[float, Person, bool, str | None] | None = None
        async for doc in self.db["people"].find(query):
            candidate = Person.model_validate(doc)
            new = person if joined else candidate
            score = match_score(person, candidate)
            if score < POSSIBLE_THRESHOLD:
                continue
            confirmed = score >= MOVE_THRESHOLD and not new.baseline
            previous = earlier.get(candidate.id)
            # Already suggested (or reviewed) for this pair: never suggest it twice. The one
            # exception is an unreviewed suggestion that we can now confirm.
            upgradable = (
                previous is not None and previous["type"] == "possible_move" and previous["status"] == "auto"
            )
            if previous is not None and not (confirmed and upgradable):
                continue
            if best is None or score > best[0]:
                best = (score, candidate, confirmed, previous["_id"] if previous else None)
        if best is None:
            return False
        score, candidate, confirmed, replaces = best
        old, new = (candidate, person) if joined else (person, candidate)
        await self.record_move(
            old, new, confidence=round(score, 3), confirmed=confirmed, source=source, replaces=replaces
        )
        return True

    async def _earlier_move_events(self, person: Person, joined: bool) -> dict[str, dict[str, Any]]:
        """Move events already recorded between `person` and someone else, by the other person's id."""
        # A move event belongs to the new profile (`person_id`) and points at the old one.
        mine, other = ("person_id", "related_person_id") if joined else ("related_person_id", "person_id")
        cursor = self.db["events"].find({mine: person.id, "type": {"$in": ["moved", "possible_move"]}})
        return {doc[other]: doc async for doc in cursor if doc.get(other)}

    async def rematch_departures(self) -> int:
        """Look again for where recent, unlinked leavers went; returns how many moves were found.

        Safe to repeat (pairs are never suggested twice). Repairs data from before the matching
        rules covered every order in which the two sides can be seen.
        """
        since = datetime.now(UTC) - MOVE_WINDOW
        query: dict[str, Any] = {
            "status": "departed",
            "departed_at": {"$gte": since},
            "moved_to": None,
            "segments": {"$ne": []},
        }
        found = 0
        async for doc in self.db["people"].find(query):
            found += await self.match_moves(Person.model_validate(doc), joined=False)
        return found

    async def record_move(
        self,
        old: Person,
        new: Person,
        confidence: float,
        confirmed: bool,
        source: str = "crawl",
        replaces: str | None = None,
    ) -> None:
        """Write the move event. `replaces` is an unreviewed `possible_move` to upgrade in place."""
        old_org = self.config.organisation(old.org_id).name
        new_org = self.config.organisation(new.org_id).name
        if confirmed:
            await link_move(self.db, old, new)
        summary = f"{new.name} moved from {old_org} to {new_org}"
        if not confirmed:
            summary += f"{POSSIBLE_MATCH_SUFFIX}, {confidence:.0%} confidence - please confirm)"
        if replaces:
            await self.db["events"].update_one(
                {"_id": replaces},
                {"$set": {"type": "moved", "summary": summary, "confidence": confidence, "source": source}},
            )
            return
        await self.emit(
            new,
            EventDraft("moved" if confirmed else "possible_move", summary, before={"org_id": old.org_id}),
            # Both sides' segments, so filtering by the segment they left still shows the move.
            segments=sorted({*old.segments, *new.segments}),
            related_person_id=old.id,
            related_org_id=old.org_id,
            confidence=confidence,
            source=source,
        )

    async def _detect_lift_out(self, person: Person, organisation: OrgConfig, now: datetime) -> None:
        """Several departures from one office in a short window is a strong market signal."""
        if not person.offices:
            return
        since = now - LIFT_OUT_WINDOW
        recent = [
            doc
            async for doc in self.db["events"].find(
                {
                    "type": "left",
                    "org_id": organisation.id,
                    "detected_at": {"$gte": since},
                    "offices": {"$in": person.offices},
                }
            )
        ]
        already = await self.db["events"].find_one(
            {
                "type": "lift_out",
                "org_id": organisation.id,
                "detected_at": {"$gte": since},
                "offices": {"$in": person.offices},
            }
        )
        if len(recent) >= 2 and already is None:
            names = ", ".join(sorted({doc["person_name"] for doc in recent}))
            await self.emit(
                person,
                EventDraft(
                    "lift_out",
                    f"{len(recent)} {self.config.domain.vocabulary.people.lower()} left {organisation.name} ({', '.join(person.offices)}) in 60 days: {names}",
                ),
            )

    # --- maintenance ------------------------------------------------------------------

    async def reprocess_org(self, org_id: str) -> dict[str, int]:
        """Re-run extraction over stored raw pages (after a parser or taxonomy fix).

        This is a data correction, not market news, so it deliberately emits no events.

        Out-of-scope people have no raw page (see `fetch_profile`), so they are re-classified
        from the minimal record in their observation instead. If that brings someone into scope,
        their full details are fetched again with the next crawl.
        """
        organisation = self.config.organisation(org_id)
        counts = {"people": 0, "updated": 0, "missing_raw": 0, "from_record": 0, "now_in_scope": 0}
        async for doc in self.db["people"].find({"org_id": organisation.id, "status": "active"}):
            counts["people"] += 1
            person = Person.model_validate(doc)
            obs = await self.db["observations"].find_one({"person_id": person.id}, sort=[("fetched_at", -1)])
            raw = self.raw_store.get(obs["raw_sha256"]) if obs and obs.get("raw_sha256") else None
            if raw is not None:
                record = extract_profile(
                    raw.decode("utf-8", "replace"),
                    person.profile_url,
                    organisation.selectors,
                    self.config.domain.fields,
                )
            elif obs is not None and obs.get("record") and not person.in_scope:
                record = ProfileRecord.model_validate(obs["record"])
                counts["from_record"] += 1
            else:
                counts["missing_raw"] += 1
                continue
            assert obs is not None  # raw is only found through an observation
            learned = await self._learned(record.groups)
            # Provenance keeps the page's real fetch date; `last_fetched` is left as it was, so a
            # reprocess doesn't make everyone look stale (or fresh).
            updated = apply_record(person, record, self.config.domain, obs["fetched_at"], learned)
            updated.last_fetched = person.last_fetched
            updated.record_hash = record.content_hash()
            if raw is None and updated.in_scope:
                # Re-classified from a partial record: clear the hash so the fetch below counts as
                # a change and stores their full details and the raw page.
                updated.record_hash = None
                counts["now_in_scope"] += 1
                await jobs.enqueue(
                    self.db,
                    "fetch",
                    f"fetch:{person.id}",
                    person.org_id,
                    {"person_id": person.id, "reason": "changed"},
                    jobs.PRIORITY_CHANGED,
                )
            if updated != person:
                await self._save(updated)
                counts["updated"] += 1
        return counts

    async def block_org(self, org_id: str, reason: str) -> None:
        log.warning("blocking %s for 24h: %s", org_id, reason)
        await self._set_org(
            org_id, status="blocked", reason=reason, blocked_until=datetime.now(UTC) + timedelta(hours=24)
        )

    async def _set_org(self, org_id: str, status: str, reason: str | None, **extra: Any) -> None:
        await self.db["organisations"].update_one(
            {"_id": org_id}, {"$set": {"status": status, "status_reason": reason, **extra}}
        )


async def erase_person(
    db: Database, profile_url: str, reason: str, raw_store: RawStore | None = None
) -> dict[str, int]:
    """Right to erasure: delete everything we hold on a person and never collect it again.

    The suppression list stores only a hash of the profile URL (the same id we use for the
    person), so it does not itself retain personal data. Their raw pages are deleted too, unless
    another observation shares the identical page (content-addressed blobs are stored once).
    Pass `raw_store` to do that; without it the next `purge_raw` removes them.
    """
    pid = person_id_for(profile_url)
    stored = await db["people"].find_one({"_id": pid}, {"profile_url": 1})
    url = stored["profile_url"] if stored else profile_url.rstrip("/")
    raw_shas = await db["observations"].distinct(
        "raw_sha256", {"person_id": pid, "raw_sha256": {"$ne": None}}
    )
    counts = {
        "people": (await db["people"].delete_many({"_id": pid})).deleted_count,
        "observations": (await db["observations"].delete_many({"person_id": pid})).deleted_count,
        "raw_pages": 0,
        # Their own events, and moves where they are the other side.
        "events": (
            await db["events"].delete_many({"$or": [{"person_id": pid}, {"related_person_id": pid}]})
        ).deleted_count,
        "briefs": (await db["ai_outputs"].delete_many({"subject_id": pid})).deleted_count,
        "notes": (await db["notes"].delete_many({"person_id": pid})).deleted_count,
    }
    await db["archive_checked"].delete_many({"person_id": pid})
    # A linked move copied their old role onto their other profile (or points at it): remove that.
    await db["people"].update_many({"history.profile_url": url}, {"$pull": {"history": {"profile_url": url}}})
    await db["people"].update_many({"moved_from": pid}, {"$set": {"moved_from": None}})
    await db["people"].update_many({"moved_to": pid}, {"$set": {"moved_to": None}})
    await db["jobs"].delete_many({"key": f"fetch:{pid}"})
    # Suppress the http and https forms too, so the same page found under the other scheme isn't
    # collected again under a different id.
    variants = {url, url.replace("https://", "http://", 1), url.replace("http://", "https://", 1)}
    for variant in variants:
        vid = person_id_for(variant)
        await db["suppressions"].replace_one(
            {"_id": vid}, {"_id": vid, "reason": reason, "at": datetime.now(UTC)}, upsert=True
        )
    if raw_store is not None:
        for sha in raw_shas:
            if await db["observations"].count_documents({"raw_sha256": sha}, limit=1) == 0:
                counts["raw_pages"] += raw_store.delete(sha)
    return counts


# A raw page is written just before its observation, so spare recent files: the purge must not
# delete a page whose observation is about to be inserted.
RAW_PURGE_GRACE = timedelta(days=1)


async def purge_raw(db: Database, raw_store: RawStore, grace: timedelta = RAW_PURGE_GRACE) -> int:
    """Delete raw pages we no longer need; returns how many were removed.

    1. Out-of-scope people keep no raw page (see `fetch_profile`). Pages stored for them before
       that rule, or while they were still in scope, are unlinked from their observations.
    2. Any page no observation refers to is deleted: erased people, unlinked pages from step 1,
       and pages whose extraction failed (spared for `grace`, long enough to inspect them).
    """
    out_of_scope = [doc["_id"] async for doc in db["people"].find({"segments": []}, {"_id": 1})]
    for start in range(0, len(out_of_scope), 1000):
        await db["observations"].update_many(
            {"person_id": {"$in": out_of_scope[start : start + 1000]}, "raw_sha256": {"$ne": None}},
            {"$set": {"raw_sha256": None}},
        )
    referenced = {
        doc["raw_sha256"]
        async for doc in db["observations"].find({"raw_sha256": {"$ne": None}}, {"raw_sha256": 1, "_id": 0})
    }
    # Listing the folder is blocking disk I/O: do it off the event loop so the worker's lanes keep going.
    candidates = await asyncio.to_thread(lambda: list(raw_store.older_than(grace.total_seconds())))
    removed = 0
    for sha in candidates:
        if sha not in referenced and raw_store.delete(sha):
            removed += 1
    if removed:
        log.info("purged %d unreferenced raw pages", removed)
    return removed


async def link_move(db: Database, old: Person, new: Person) -> None:
    """Carry the career history over to the person's new profile."""
    snapshot = RoleSnapshot(
        org_id=old.org_id,
        title=old.title,
        offices=old.offices,
        groups=old.groups_raw,
        profile_url=old.profile_url,
        first_seen=old.first_seen,
        last_seen=old.departed_at,
    )
    history = [h.model_dump() for h in [*old.history, snapshot]]
    await db["people"].update_one({"_id": new.id}, {"$set": {"moved_from": old.id, "history": history}})
    await db["people"].update_one({"_id": old.id}, {"$set": {"moved_to": new.id}})


async def sync_config(db: Database, config: AppConfig) -> None:
    """Seed organisations and the domain profile from YAML, then load the live copies (incl. admin
    edits) from Mongo."""
    await seed_orgs(db, list(config.organisations))
    await refresh_orgs(db, config)
    await seed_domain(db, config.domain_name, config.domain)
    await refresh_domain(db, config)
