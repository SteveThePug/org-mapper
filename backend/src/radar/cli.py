"""Command line: `radar <command>` (the root Makefile wraps the common ones)."""

import argparse
import asyncio
import getpass
import gzip
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from bson import json_util

from radar.auth import create_user
from radar.config import get_config
from radar.db import close_db, ensure_indexes, get_db
from radar.llm.classify import LabelClassifier
from radar.llm.gateway import LlmGateway
from radar.scraping.fetcher import Blocked, CrawlerNotIdentified, Fetcher
from radar.scraping.raw_store import RawStore
from radar.scraping.survey import survey_organisation
from radar.settings import get_settings
from radar.worker import jobs
from radar.worker.crawler import Crawler, FetchError, erase_person, sync_config

Command = Callable[[argparse.Namespace], Awaitable[None]]

# What a demo snapshot contains. Users, sessions, usage logs and the job queue are excluded.
SNAPSHOT_COLLECTIONS = (
    "organisations",
    "domain_profiles",
    "people",
    "events",
    "observations",
    "scrape_runs",
    "ai_outputs",
    "label_tags",
    "suppressions",
)


def _crawler() -> tuple[Crawler, Fetcher]:
    settings = get_settings()
    fetcher = Fetcher.from_settings(settings)
    labels = LabelClassifier(get_db(), LlmGateway(get_db(), settings))
    crawler = Crawler(get_db(), fetcher, get_config(), settings, RawStore(settings.raw_dir), labels)
    return crawler, fetcher


async def cmd_init(args: argparse.Namespace) -> None:
    db = get_db()
    await ensure_indexes(db)
    await sync_config(db, get_config())
    print("indexes and organisations are in place")


async def cmd_survey(args: argparse.Namespace) -> None:
    config = get_config()
    organisations = (
        [config.organisation(f) for f in args.organisations] if args.organisations else config.organisations
    )
    fetcher = Fetcher.from_settings(get_settings())
    try:
        results = await asyncio.gather(
            *(survey_organisation(fetcher, organisation) for organisation in organisations)
        )
    finally:
        await fetcher.aclose()
    for res in results:
        print(f"\n=== {res.org_id}: {res.verdict}")
        print(f"  robots.txt      : {res.robots}")
        print(f"  sitemaps        : {', '.join(res.sitemaps[:4]) or '-'}")
        if res.people_sitemaps:
            print(f"  people sitemaps : {', '.join(res.people_sitemaps[:4])}")
        if res.configured_count is not None:
            print(f"  configured discovery found {res.configured_count} profile URLs")
        print(f"  profile URLs    : {res.profile_count} under {res.profile_prefix or '-'}")
        if res.suggested_include() and res.configured_count is None:
            print(f"  suggested include regex: {res.suggested_include()}")
        if res.sample_url:
            print(f"  sample          : {res.sample_url}")
            print(f"  fields (method) : {res.sample_fields}")
            for key, value in res.sample_values.items():
                print(f"      {key:<10}: {str(value)[:110]}")
        for error in res.errors[:5]:
            print(f"  ! {error}")


async def cmd_scrape(args: argparse.Namespace) -> None:
    """One-off crawl of one organisation in the foreground: discovery, then up to LIMIT profile fetches."""
    db = get_db()
    await ensure_indexes(db)
    await sync_config(db, get_config())
    crawler, fetcher = _crawler()
    try:
        run = await crawler.discover_org(args.organisation, limit=args.limit)
        print({k: v for k, v in run.items() if k not in ("_id", "started_at", "finished_at")})
        processed = 0
        while args.limit is None or processed < args.limit:
            job = await jobs.claim(db, set(), only_org=args.organisation)
            if job is None:
                break
            person_id = job["payload"]["person_id"]
            try:
                note = await crawler.fetch_profile(person_id, job["payload"].get("reason", "new"))
                await jobs.complete(db, job, note)
                processed += 1
                print(f"[{processed}] {person_id}: {note}")
            except FetchError as exc:
                await jobs.fail(db, job, str(exc))
            except Blocked as exc:
                await crawler.block_org(args.organisation, str(exc))
                await jobs.fail(db, job, str(exc))
                print(f"blocked: {exc}")
                break
    finally:
        await fetcher.aclose()


async def cmd_backfill(args: argparse.Namespace) -> None:
    from radar.backfill import Backfiller

    db = get_db()
    await sync_config(db, get_config())
    crawler, fetcher = _crawler()
    organisations = args.organisations or [f.id for f in get_config().organisations if f.discovery]
    try:
        for org_id in organisations:
            result = await Backfiller(crawler).run(org_id, args.months, args.max_pages, args.force)
            print(org_id, result)
            if result.get("remaining"):
                print(
                    f"  {result['remaining']} archived pages still to read: run the backfill again to continue"
                )
        print("moves found by re-matching recent departures:", await crawler.rematch_departures())
    finally:
        await fetcher.aclose()


async def cmd_rematch(args: argparse.Namespace) -> None:
    db = get_db()
    await sync_config(db, get_config())
    crawler, fetcher = _crawler()
    try:
        print("moves found:", await crawler.rematch_departures())
    finally:
        await fetcher.aclose()


async def cmd_reprocess(args: argparse.Namespace) -> None:
    crawler, fetcher = _crawler()
    try:
        print(await crawler.reprocess_org(args.organisation))
    finally:
        await fetcher.aclose()


async def cmd_create_user(args: argparse.Namespace) -> None:
    password = args.password or getpass.getpass(f"Password for {args.email}: ")
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters")
    await create_user(get_db(), args.email, password, args.role)
    print(f"user {args.email} ({args.role}) saved")


async def cmd_forget(args: argparse.Namespace) -> None:
    counts = await erase_person(get_db(), args.profile_url, args.reason, RawStore(get_settings().raw_dir))
    print(f"erased {counts}; the profile is on the suppression list and will not be collected again")


async def cmd_snapshot_export(args: argparse.Namespace) -> None:
    db = get_db()
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as out:
        for name in SNAPSHOT_COLLECTIONS:
            count = 0
            async for doc in db[name].find():
                out.write(json.dumps({"c": name, "d": json.loads(json_util.dumps(doc))}) + "\n")
                count += 1
            print(f"{name}: {count}")
    print(f"snapshot written to {path}")


async def cmd_snapshot_import(args: argparse.Namespace) -> None:
    db = get_db()
    await ensure_indexes(db)
    counts: dict[str, int] = {}
    with gzip.open(args.path, "rt", encoding="utf-8") as src:
        for line in src:
            row = json.loads(line)
            doc = json_util.loads(json.dumps(row["d"]))
            await db[row["c"]].replace_one({"_id": doc["_id"]}, doc, upsert=True)
            counts[row["c"]] = counts.get(row["c"], 0) + 1
    await ensure_indexes(db)  # again: upgrades documents from snapshots taken by older versions
    await sync_config(db, get_config())
    print(counts)


async def cmd_openapi(args: argparse.Namespace) -> None:
    from radar.api.main import app  # imported lazily: only this command needs the web app

    Path(args.path).write_text(json.dumps(app.openapi(), indent=2))
    print(f"OpenAPI schema written to {args.path}")


COMMANDS: dict[str, Command] = {
    "init": cmd_init,
    "survey": cmd_survey,
    "scrape": cmd_scrape,
    "reprocess": cmd_reprocess,
    "backfill": cmd_backfill,
    "rematch": cmd_rematch,
    "create-user": cmd_create_user,
    "forget": cmd_forget,
    "snapshot-export": cmd_snapshot_export,
    "snapshot-import": cmd_snapshot_import,
    "openapi": cmd_openapi,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create indexes and organisation records")
    survey = sub.add_parser("survey", help="check robots.txt / sitemaps / structured data for organisations")
    survey.add_argument("organisations", nargs="*", help="organisation ids (default: all)")
    scrape = sub.add_parser("scrape", help="crawl one organisation now, in the foreground")
    scrape.add_argument("organisation")
    scrape.add_argument("--limit", type=int, default=None, help="max profile fetches")
    backfill = sub.add_parser("backfill", help="reconstruct recent moves from Internet Archive snapshots")
    backfill.add_argument("organisations", nargs="*", help="organisation ids (default: all configured)")
    backfill.add_argument("--months", type=int, default=6)
    backfill.add_argument(
        "--max-pages", type=int, default=300, help="archived pages to read per organisation"
    )
    backfill.add_argument("--force", action="store_true")
    sub.add_parser("rematch", help="look again for where recent leavers went (no network)")
    reprocess = sub.add_parser("reprocess", help="re-parse stored raw pages for an organisation (no network)")
    reprocess.add_argument("organisation")
    user = sub.add_parser("create-user", help="create or update a user")
    user.add_argument("email")
    user.add_argument("--role", choices=["recruiter", "admin"], default="recruiter")
    user.add_argument("--password", help="omit to be prompted")
    forget = sub.add_parser("forget", help="erase a person and suppress future collection (GDPR erasure)")
    forget.add_argument("profile_url")
    forget.add_argument("--reason", default="erasure request")
    export = sub.add_parser("snapshot-export", help="export data to a .jsonl.gz snapshot")
    export.add_argument("path")
    load = sub.add_parser("snapshot-import", help="load a .jsonl.gz snapshot")
    load.add_argument("path")
    openapi = sub.add_parser("openapi", help="write the API's OpenAPI schema (for frontend types)")
    openapi.add_argument("path")
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        await COMMANDS[args.command](args)
    except CrawlerNotIdentified as exc:
        raise SystemExit(str(exc)) from None
    finally:
        await close_db()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
