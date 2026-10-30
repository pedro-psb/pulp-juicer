COMPOSE ?= docker compose
UV ?= uv
QUARTO ?= quarto
PROJECT ?= bucket-loadtest
PULPCORE_REPO ?= ../pulpcore
PULP_MAVEN_REPO ?= ../pulp_maven
RUN_ID ?=
FIXTURE_ID ?= shared
COUNT ?= 650
PAYLOAD_BYTES ?= 65536
UPSTREAM_URL ?= http://fixture:8000/
PULP_READY_TIMEOUT ?= 300
CANDIDATE ?=
USERS ?= 10
SPAWN_RATE ?= 10
DURATION ?= 5m
WAIT_SECONDS ?= 0
IDLE_SECONDS ?= 5
TAIL_SECONDS ?= 5
RUN_DIR = _results/_run/$(RUN_ID)
CANDIDATE_DIR = _results/$(CANDIDATE)/$(RUN_ID)
COMPARISON_DIR = _results/_comparison/$(RUN_ID)

export PULPCORE_REPO PULP_MAVEN_REPO RUN_ID

.DEFAULT_GOAL := help
.PHONY: help _up _setup_pulp status destroy clean check-run-id check-sources fixture up run run-all plot report

help: ## Show available targets and overrides.
	@printf 'Usage: make <target> [VARIABLE=value ...]\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nOverrides: COMPOSE, UV, QUARTO, PROJECT, PULPCORE_REPO, PULP_MAVEN_REPO, RUN_ID, FIXTURE_ID, COUNT, PAYLOAD_BYTES, UPSTREAM_URL, PULP_READY_TIMEOUT, CANDIDATE, USERS, SPAWN_RATE, DURATION, WAIT_SECONDS, IDLE_SECONDS, TAIL_SECONDS\n'

check-sources:
	$(UV) run --locked python check_sources.py --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)"

check-run-id:
	@test -n "$(RUN_ID)" || { printf 'Set RUN_ID for this run, for example: RUN_ID=run002 make up\n' >&2; exit 2; }

fixture: ## Generate the shared Maven fixture if needed.
	@test -f fixture/manifest.json || $(UV) run --locked python make_fixture.py --output fixture --fixture-id "$(FIXTURE_ID)" --count "$(COUNT)" --payload-bytes "$(PAYLOAD_BYTES)"

_up: check-run-id check-sources
	$(MAKE) fixture FIXTURE_ID="$(FIXTURE_ID)" COUNT="$(COUNT)" PAYLOAD_BYTES="$(PAYLOAD_BYTES)"
	$(COMPOSE) -p $(PROJECT) -f compose.yaml up -d --build

_setup_pulp: _up
	UPSTREAM_URL="$(UPSTREAM_URL)" PULP_READY_TIMEOUT="$(PULP_READY_TIMEOUT)" ./setup_pulp.sh
	$(UV) run --locked python run_metadata.py setup --run-dir "$(RUN_DIR)" --manifest fixture/manifest.json --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)" --count "$(COUNT)" --fixture-id "$(FIXTURE_ID)" --payload-bytes "$(PAYLOAD_BYTES)" --project "$(PROJECT)" --upstream-url "$(UPSTREAM_URL)"

up: destroy _setup_pulp ## Build Pulp, start services, and create repositories.

run: check-run-id ## Run one Locust candidate into its results directory.
	@case "$(CANDIDATE)" in normal|staging) ;; *) printf 'CANDIDATE must be normal or staging.\n' >&2; exit 2;; esac
	@test -f fixture/manifest.json && test -f "$(RUN_DIR)/setup.json" || { printf 'Missing shared fixture or setup for RUN_ID=%s. Run make up RUN_ID=%s first.\n' "$(RUN_ID)" "$(RUN_ID)" >&2; exit 2; }
	@test -f "$(RUN_DIR)/setup_metadata.json" || { printf 'Missing setup metadata for RUN_ID=%s. Run make up RUN_ID=%s first.\n' "$(RUN_ID)" "$(RUN_ID)" >&2; exit 2; }
	$(UV) run --locked python run_metadata.py run --setup-dir "$(RUN_DIR)" --manifest fixture/manifest.json --output-dir "$(CANDIDATE_DIR)" --candidate "$(CANDIDATE)" --users "$(USERS)" --spawn-rate "$(SPAWN_RATE)" --duration "$(DURATION)" --wait-seconds "$(WAIT_SECONDS)" --idle-seconds "$(IDLE_SECONDS)" --tail-seconds "$(TAIL_SECONDS)"
	CANDIDATE="$(CANDIDATE)" WAIT_SECONDS="$(WAIT_SECONDS)" IDLE_SECONDS="$(IDLE_SECONDS)" TAIL_SECONDS="$(TAIL_SECONDS)" REQUESTS_CSV="$(CANDIDATE_DIR)/requests.csv" DB_CPU_CSV="$(CANDIDATE_DIR)/db_cpu.csv" DB_CPU_COMPOSE="$(COMPOSE)" DB_CPU_PROJECT="$(PROJECT)" DB_CPU_COMPOSE_FILE="$(abspath compose.yaml)" $(UV) run --locked locust -f locustfile.py --headless -u "$(USERS)" -r "$(SPAWN_RATE)" -t "$(DURATION)" --csv "$(CANDIDATE_DIR)/locust"
	@printf 'Results: %s\n' "$(abspath $(CANDIDATE_DIR))"

run-all: check-run-id ## Set up Pulp, run both candidates sequentially, and render reports.
	$(MAKE) up
	$(MAKE) run CANDIDATE=normal
	$(MAKE) up
	$(MAKE) run CANDIDATE=staging
	$(MAKE) report

plot: check-run-id ## Plot both Locust candidates for this run ID.
	$(UV) run --locked python plot_locust_results.py --input _results --run-id "$(RUN_ID)" --output "$(COMPARISON_DIR)"

report: ## Render one Quarto report for all completed candidate runs.
	$(UV) run --locked --group report python generate_reports.py --results _results --quarto "$(QUARTO)"

clean: ## Remove all run results; keep the shared fixture and Pulp volumes.
	rm -rf -- _results

status: ## Show container status.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml ps

destroy: ## Stop containers and remove Pulp volumes.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down --volumes --remove-orphans
