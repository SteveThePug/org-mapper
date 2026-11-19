"""Everything the background worker runs, kept in one place.

- `runner.py`  - the worker process: scheduler loop, lanes that claim jobs, startup/shutdown.
- `crawler.py` - what a job actually does: discover, fetch, extract, diff, reprocess, purge.
- `jobs.py`    - the Mongo-backed job queue (enqueue, claim, complete, fail, requeue).

Start it with `python -m radar.worker` (that runs `__main__.py`).

The worker also uses shared code that lives outside this package because the API and CLI need
it too: `scraping/`, `pipeline/`, `llm/`, `db.py`, `config.py`, `settings.py`, `models.py`,
`orgs.py`, `domain.py`.

This file deliberately imports nothing, so `from radar.worker import jobs` (used by the API)
doesn't drag in the whole worker process.
"""
