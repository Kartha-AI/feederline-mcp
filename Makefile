PY            ?= uv run python
PG_PORT       ?= 5433
MCP_PORT      ?= 8000
ADMIN_URL     ?= postgresql://grid:grid@localhost:$(PG_PORT)/grid
LOADER_URL    ?= postgresql://grid_load:grid_load@localhost:$(PG_PORT)/grid
WRITER_URL    ?= postgresql://grid_write:grid_write@localhost:$(PG_PORT)/grid
PSQL          ?= docker compose exec -T postgres psql -U grid -d grid -v ON_ERROR_STOP=1
ADMIN         ?= ADMIN_DATABASE_URL=$(ADMIN_URL) LOADER_DATABASE_URL=$(LOADER_URL) WRITER_DATABASE_URL=$(WRITER_URL) \
                 uv run feederline-mcp-admin
HOT_DAYS      ?= 14
HISTORY_DIR   ?= data/parquet
YESTERDAY     := $(shell date -v-1d +%F 2>/dev/null || date -d yesterday +%F)

.PHONY: kafka ha-check up down demo status npm-build version-check load-ieee load-smartds load-hifld load-pecan sim rotate refresh-rollups archive test test-unit inspect lint fixtures

up:            ## compose up with migrations
	PG_PORT=$(PG_PORT) MCP_PORT=$(MCP_PORT) docker compose up -d --build --wait postgres mcp-a mcp-b proxy

down:          ## down, volumes kept
	docker compose down

load-ieee:     ## IEEE 13/34/123 as CIM, registry, latest values, edges
	$(ADMIN) load ieee

load-smartds:  ## REGION=P1R SUBSTATION=p1rhs0_1247 [DATE=YYYY-MM-DD MAX_PROFILES=40]
	$(ADMIN) load smartds --region $(or $(REGION),P1R) --substation $(or $(SUBSTATION),p1rhs0_1247) \
	  $(if $(DATE),--date $(DATE) --max-profiles $(or $(MAX_PROFILES),40))

load-hifld:    ## STATE=CA
	$(ADMIN) load hifld --state $(or $(STATE),CA)

demo:          ## deterministic demo telemetry for the last 30 days (seed 42), then a rollup refresh
	$(ADMIN) simulate --backfill-days $(or $(DAYS),30) --seed 42

status:        ## migration, feeders loaded, telemetry range
	$(ADMIN) status

load-pecan:    ## FILE=... REGION=IEEE123
	DATABASE_URL=$(LOADER_URL) HISTORY_DIR=$(HISTORY_DIR) $(PY) -m loaders.pecan --file $(FILE) --region $(or $(REGION),IEEE123)
	$(MAKE) refresh-rollups

sim:
	docker compose --profile sim up -d simulator

rotate:        ## create D+1, D+2 partitions; drop older than HOT_DAYS once archived; ANALYZE
	DATABASE_URL=$(ADMIN_URL) HISTORY_DIR=$(HISTORY_DIR) $(PY) -m ingest.rotate --hot-days $(HOT_DAYS)

refresh-rollups:
	$(ADMIN) refresh-rollups

archive:       ## yesterday's partition -> HISTORY_DIR/measurement_value/YYYY-MM-DD.parquet
	DATABASE_URL=$(ADMIN_URL) HISTORY_DIR=$(HISTORY_DIR) $(PY) -m ingest.archive --date $(YESTERDAY)

fixtures:      ## regenerate tests/fixtures/ieee<N>_solution.json from the vendored DSS masters
	$(PY) -m loaders.ieee --fixtures-only --feeders 13 34 123

test:          ## unit + contract + golden/db (needs make up && make load-ieee)
	LOADER_URL=$(LOADER_URL) DATABASE_URL=postgresql://mcp:mcp@localhost:$(PG_PORT)/grid WRITER_URL=$(WRITER_URL) ADMIN_URL=$(ADMIN_URL) uv run pytest -q $(PYTEST_ARGS)

test-unit:
	uv run pytest -q -m "not db"

INSPECT_BROWSER ?= Google Chrome

inspect:       ## MCP Inspector UI in INSPECT_BROWSER (Safari's HTTPS-Only mode refuses the http://localhost UI); npm_config_os overrides an os= in ~/.npmrc
	MCP_AUTO_OPEN_ENABLED=false npm_config_os=$$(node -p process.platform) npx -y @modelcontextprotocol/inspector \
	  --transport http --server-url http://localhost:$(MCP_PORT)/mcp --header "Authorization: Bearer $(or $(MCP_TOKEN),dev-token)" \
	| while IFS= read -r line; do \
	    printf '%s\n' "$$line"; \
	    url=$$(printf '%s' "$$line" | grep -oE 'http://[^ ]*MCP_INSPECTOR_API_TOKEN=[0-9a-f]+'); \
	    if [ -n "$$url" ]; then open -a "$(INSPECT_BROWSER)" "$$url" || echo "open $$url in a browser without HTTPS-Only"; fi; \
	  done

lint:
	uv run ruff check . && uv run lint-imports

kafka:         ## Redpanda + the Kafka writer (compose profile kafka; host port 19092)
	docker compose --profile kafka up -d --wait redpanda kafka-writer

ha-check:      ## two replicas behind nginx: stop the one holding a session, the other must serve it
	MCP_PORT=$(MCP_PORT) uv run python deploy/failover_check.py

npm-build:     ## build and test the npm CLI (npm/)
	cd npm && npm ci && npm run build && npm test

version-check: ## the Python package, npm package and image tags share one version
	uv run python scripts/check_versions.py
