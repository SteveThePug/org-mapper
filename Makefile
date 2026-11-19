# Partner Radar - every common task is one `make` target. `make help` lists them.

COMPOSE  := docker compose
DEV      := docker compose -f docker-compose.yml -f docker-compose.dev.yml
PROD     := docker compose -f docker-compose.yml -f docker-compose.prod.yml
STPVM    := docker compose -f docker-compose.yml -f docker-compose.stpvm.yml
CLI      := $(COMPOSE) run --rm api radar
SNAPSHOT := seed/snapshot.jsonl.gz
ENV_GET   = $$(sed -n 's/^$(1)=//p' $(CURDIR)/.env | tr -d '"')
LOCAL_MONGO = mongodb://radar:$(call ENV_GET,MONGO_APP_PASSWORD)@localhost:27017/radar?authSource=radar
TEST_MONGO  = mongodb://root:$(call ENV_GET,MONGO_ROOT_PASSWORD)@localhost:27017/?authSource=admin

.DEFAULT_GOAL := help
.PHONY: help env install crawl-contact demo up down logs prod survey scrape backfill rematch reprocess user forget snapshot seed \
        stpvm dev-db dev-api dev-worker dev-web types fmt check test backup restore

help: ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# --- setup ------------------------------------------------------------------------------------

env: ## Create/complete .env with random secrets: make env [OPENROUTER_API_KEY=sk-or-...]
	@python3 scripts/make_env.py $(OPENROUTER_API_KEY)

install: ## Install local toolchains (uv + npm) and the git pre-commit hooks
	cd backend && uv sync
	cd frontend && npm ci --no-audit --no-fund
	cd backend && uv run pre-commit install

# Crawling contacts real websites, so it needs the operator's own contact in the user agent.
# The worker and CLI enforce the same rule (scraping/fetcher.py: check_user_agent); this just
# fails early with a clear message instead of starting a worker that refuses to crawl.
crawl-contact:
	@case "$(call ENV_GET,SCRAPER_USER_AGENT)" in \
		*example.com*|*change-me*|"") \
			echo "Set SCRAPER_USER_AGENT in .env to include your own contact before crawling,"; \
			echo 'e.g. SCRAPER_USER_AGENT="PartnerRadarBot/0.1 (+mailto:you@yourdomain.com)"'; \
			exit 1;; \
	esac

# --- run with Docker --------------------------------------------------------------------------

demo: .env ## Start the app (http://localhost:8080) with demo data; never contacts the organisations' sites
	$(COMPOSE) up -d --build --wait
	@if [ -f $(SNAPSHOT) ]; then \
		$(COMPOSE) run --rm -v ./seed:/seed:ro api radar snapshot-import /seed/$(notdir $(SNAPSHOT)); \
	else \
		echo "No $(SNAPSHOT) found, so the app starts empty. The demo never crawls on its own:"; \
		echo "to collect data from the organisations' websites yourself, set SCRAPER_USER_AGENT and run 'make up'."; \
	fi
	@echo "Open http://localhost:$(or $(call ENV_GET,WEB_PORT),8080) - admin login is BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD in .env"

up: .env crawl-contact ## Start everything including the background crawler
	$(COMPOSE) --profile crawler up -d --build --wait

down: ## Stop everything (data is kept in Docker volumes)
	$(COMPOSE) --profile crawler down

logs: ## Follow logs
	$(COMPOSE) --profile crawler logs -f --tail=100

prod: .env crawl-contact ## Public deployment: Caddy on 80/443 with TLS for SITE_ADDRESS
	$(PROD) --profile crawler up -d --build --wait

stpvm: .env crawl-contact ## Behind the web_server edge on stpvm (radar.adam-french.co.uk); publishes no port
	$(STPVM) --profile crawler up -d --build --wait

# --- operations -------------------------------------------------------------------------------

survey: ## Onboarding check of organisation(s): make survey [ORG=weil]
	$(CLI) survey $(ORG)

scrape: ## One-off foreground crawl: make scrape ORG=weil [LIMIT=20]
	$(CLI) scrape $(ORG) $(if $(LIMIT),--limit $(LIMIT))

backfill: ## Rebuild recent history from the Internet Archive: make backfill [ORG=weil] [MONTHS=6]
	$(CLI) backfill $(ORG) $(if $(MONTHS),--months $(MONTHS))

rematch: ## Look again for where recent leavers went (no network)
	$(CLI) rematch

reprocess: ## Re-parse stored pages after a parser/config fix (no network): make reprocess ORG=weil
	$(CLI) reprocess $(ORG)

user: ## Create a user: make user EMAIL=jo@organisation.com [ROLE=admin]
	$(CLI) create-user $(EMAIL) --role $(or $(ROLE),recruiter)

forget: ## GDPR erasure + suppression: make forget URL=https://organisation.com/people/name
	$(CLI) forget "$(URL)"

snapshot: ## Export the current data to seed/snapshot.jsonl.gz
	mkdir -p seed
	$(COMPOSE) run --rm --user "$$(id -u):$$(id -g)" -v ./seed:/seed api radar snapshot-export /seed/$(notdir $(SNAPSHOT))

seed: ## Load seed/snapshot.jsonl.gz into the database
	$(COMPOSE) run --rm -v ./seed:/seed:ro api radar snapshot-import /seed/$(notdir $(SNAPSHOT))

backup: ## Dump the database to backups/
	mkdir -p backups
	$(COMPOSE) exec -T mongo sh -c 'mongodump --archive --gzip -u root -p "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --db radar' > backups/radar-$$(date +%Y%m%d-%H%M).archive.gz
	@ls -lh backups | tail -1

restore: ## Restore a dump: make restore FILE=backups/radar-....archive.gz
	$(COMPOSE) exec -T mongo sh -c 'mongorestore --archive --gzip --drop -u root -p "$$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin' < $(FILE)

# --- local development (hot reload; Mongo in Docker) -------------------------------------------

dev-db: .env ## Start only MongoDB, exposed on localhost:27017
	$(DEV) up -d --wait mongo

dev-api: dev-db ## Run the API with reload on :8000
	cd backend && MONGO_URI="$(LOCAL_MONGO)" uv run uvicorn radar.api.main:app --reload --port 8000

dev-worker: dev-db ## Run the crawler worker locally
	cd backend && MONGO_URI="$(LOCAL_MONGO)" uv run python -m radar.worker

dev-web: ## Run the frontend dev server on :5173 (proxies /api to :8000)
	cd frontend && npm run dev

types: ## Regenerate frontend TypeScript types from the API schema
	cd backend && uv run radar openapi ../frontend/openapi.json
	cd frontend && npx -y -p openapi-typescript@7 -p typescript@5 openapi-typescript openapi.json -o src/api/schema.d.ts

fmt: ## Format code
	cd backend && uv run black src tests && uv run ruff check --fix src tests

check: ## Lint + type-check backend and frontend
	cd backend && uv run black --check src tests && uv run ruff check src tests && uv run mypy
	cd frontend && npx tsc -b && npx oxlint src

test: dev-db ## Run the test suite (unit + integration against a throwaway Mongo database)
	cd backend && TEST_MONGO_URI="$(TEST_MONGO)" uv run pytest -q

.env:
	@python3 scripts/make_env.py $(OPENROUTER_API_KEY)
