COMPOSE ?= docker compose
UV ?= uv
PROJECT ?= bucket-loadtest
PULPCORE_REPO ?= ../pulpcore
PULP_MAVEN_REPO ?= ../pulp_maven
RUN_ID ?=
FIXTURE_ID ?= shared
COUNT ?= 1000
PAYLOAD_BYTES ?= 65536
UPSTREAM_URL ?= http://fixture:8000/
PULP_READY_TIMEOUT ?= 300
SCENARIO ?= normal
USERS ?= 10
SPAWN_RATE ?= 10
DURATION ?= 5m
WAIT_SECONDS ?= 0
RUN_DIR = _results/_run/$(RUN_ID)
SCENARIO_DIR = _results/$(SCENARIO)/$(RUN_ID)
COMPARISON_DIR = _results/_comparison/$(RUN_ID)

export PULPCORE_REPO PULP_MAVEN_REPO RUN_ID

.DEFAULT_GOAL := help
.PHONY: help up down status destroy clean check-run-id check-sources fixture setup run plot

help: ## Show available targets and overrides.
	@printf 'Usage: make <target> [VARIABLE=value ...]\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nOverrides: COMPOSE, UV, PROJECT, PULPCORE_REPO, PULP_MAVEN_REPO, RUN_ID, FIXTURE_ID, COUNT, PAYLOAD_BYTES, UPSTREAM_URL, PULP_READY_TIMEOUT, SCENARIO, USERS, SPAWN_RATE, DURATION, WAIT_SECONDS\n'

check-sources:
	$(UV) run --locked python check_sources.py --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)"

check-run-id:
	@test -n "$(RUN_ID)" || { printf 'Set RUN_ID for this run, for example: RUN_ID=run002 make up\n' >&2; exit 2; }

fixture: ## Generate the shared Maven fixture if needed.
	@test -f fixture/manifest.json || $(UV) run --locked python make_fixture.py --output fixture --fixture-id "$(FIXTURE_ID)" --count "$(COUNT)" --payload-bytes "$(PAYLOAD_BYTES)"

up: check-run-id check-sources ## Build and start Pulp and the fixture server.
	$(MAKE) fixture FIXTURE_ID="$(FIXTURE_ID)" COUNT="$(COUNT)" PAYLOAD_BYTES="$(PAYLOAD_BYTES)"
	$(COMPOSE) -p $(PROJECT) -f compose.yaml up -d --build

setup: check-run-id ## Create Pulp repositories for this run ID.
	@test -f fixture/manifest.json || { printf 'Missing shared fixture. Run make up RUN_ID=%s first.\n' "$(RUN_ID)" >&2; exit 2; }
	UPSTREAM_URL="$(UPSTREAM_URL)" PULP_READY_TIMEOUT="$(PULP_READY_TIMEOUT)" ./setup_pulp.sh

run: check-run-id ## Run one Locust scenario into its results directory.
	@test -z "$(MODE)" || { printf 'MODE was renamed to SCENARIO; use SCENARIO=normal or SCENARIO=staging.\n' >&2; exit 2; }
	@case "$(SCENARIO)" in normal|staging) ;; *) printf 'SCENARIO must be normal or staging.\n' >&2; exit 2;; esac
	@test -f fixture/manifest.json && test -f "$(RUN_DIR)/setup.json" || { printf 'Missing shared fixture or setup for RUN_ID=%s. Run make up RUN_ID=%s and make setup RUN_ID=%s first.\n' "$(RUN_ID)" "$(RUN_ID)" "$(RUN_ID)" >&2; exit 2; }
	@if test -f "_results/$(RUN_ID)/fixture/manifest.json" && ! cmp -s fixture/manifest.json "_results/$(RUN_ID)/fixture/manifest.json"; then printf 'RUN_ID=%s used a different legacy fixture. Start a new run ID for comparable scenarios.\n' "$(RUN_ID)" >&2; exit 2; fi
	@mkdir -p "$(SCENARIO_DIR)"
	SCENARIO="$(SCENARIO)" WAIT_SECONDS="$(WAIT_SECONDS)" DB_CPU_CSV="$(SCENARIO_DIR)/db_cpu.csv" DB_CPU_COMPOSE="$(COMPOSE)" DB_CPU_PROJECT="$(PROJECT)" DB_CPU_COMPOSE_FILE="$(abspath compose.yaml)" $(UV) run --locked locust -f locustfile.py --headless -u "$(USERS)" -r "$(SPAWN_RATE)" -t "$(DURATION)" --csv "$(SCENARIO_DIR)/locust" --html "$(SCENARIO_DIR)/report.html"
	@printf 'Results: %s\n' "$(abspath $(SCENARIO_DIR))"

plot: check-run-id ## Plot both Locust scenarios for this run ID.
	$(UV) run --locked python plot_locust_results.py --input _results --run-id "$(RUN_ID)" --output "$(COMPARISON_DIR)"

clean: ## Remove all run results; keep the shared fixture and Pulp volumes.
	rm -rf -- _results

down: ## Stop containers and keep Pulp data.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down

status: ## Show container status.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml ps

destroy: ## Stop containers and remove Pulp volumes.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down --volumes --remove-orphans
