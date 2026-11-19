# Manual setup

Runs the whole stack locally with Docker. The repository ships without data, so the worker has to crawl the firms' websites before anything appears.

## Requirements
- Docker with Docker Compose
- `make` and `python3`
- An [OpenRouter](https://openrouter.ai) API key (optional, the AI features are disabled without one)

## Steps
1. Create the `.env` file. This generates random passwords and secrets, and adds your API key if you have one:
   ```sh
   make env OPENROUTER_API_KEY=sk-or-...
   ```
2. Open `.env` and set `SCRAPER_USER_AGENT` to include your own contact details. Crawling is refused until you do, so the firms can reach whoever is crawling them:
   ```sh
   SCRAPER_USER_AGENT="PartnerRadarBot/0.1 (+mailto:you@yourdomain.com)"
   ```
3. Start everything, including the crawler:
   ```sh
   make up
   ```
4. Open http://localhost:8080 and log in with `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` from `.env`.

## While it runs
The first crawl takes a while, because the scraper waits at least 3 seconds between requests to each site. Partners appear as their profiles are fetched.
- `make logs` follows the progress.
- `make backfill` rebuilds recent history from the Internet Archive, so the moves feed isn't empty on day one.
- `make down` stops everything. The data is kept in Docker volumes.

`make help` lists every other command.
