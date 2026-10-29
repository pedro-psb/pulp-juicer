COMPOSE ?= docker compose
UV ?= uv
PROJECT ?= bucket-loadtest
PULPCORE_REPO ?= ../pulpcore
PULP_MAVEN_REPO ?= ../pulp_maven
RUN_ID ?= local001
COUNT ?= 1000
PAYLOAD_BYTES ?= 65536
UPSTREAM_URL ?= http://fixture:8000/
MODE ?= normal
USERS ?= 10
SPAWN_RATE ?= 10
DURATION ?= 5m
WAIT_SECONDS ?= 0
RESULTS_DIR = _results/$(RUN_ID)

export PULPCORE_REPO PULP_MAVEN_REPO RUN_ID

.DEFAULT_GOAL := help
.PHONY: help up down status destroy check-sources fixture setup run plot

help: ## Show available targets and overrides.
	@printf 'Usage: make <target> [VARIABLE=value ...]\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nOverrides: COMPOSE, UV, PROJECT, PULPCORE_REPO, PULP_MAVEN_REPO, RUN_ID, COUNT, PAYLOAD_BYTES, UPSTREAM_URL, MODE, USERS, SPAWN_RATE, DURATION, WAIT_SECONDS\n'

check-sources:
	$(UV) run --locked python check_sources.py --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)"

fixture: ## Generate Maven fixture artifacts.
	$(UV) run --locked python make_fixture.py --output "$(RESULTS_DIR)/fixture" --run-id "$(RUN_ID)" --count "$(COUNT)" --payload-bytes "$(PAYLOAD_BYTES)"

up: check-sources ## Build and start Pulp and the fixture server.
	@test -f "$(RESULTS_DIR)/fixture/manifest.json" || $(MAKE) fixture RUN_ID="$(RUN_ID)" COUNT="$(COUNT)" PAYLOAD_BYTES="$(PAYLOAD_BYTES)"
	$(COMPOSE) -p $(PROJECT) -f compose.yaml up -d --build

setup: ## Create Pulp repositories for this run ID.
	UPSTREAM_URL="$(UPSTREAM_URL)" ./setup_pulp.sh

run: ## Run one Locust mode into this run ID's results directory.
	@mkdir -p "$(RESULTS_DIR)"
	MODE="$(MODE)" WAIT_SECONDS="$(WAIT_SECONDS)" $(UV) run --locked locust -f locustfile.py --headless -u "$(USERS)" -r "$(SPAWN_RATE)" -t "$(DURATION)" --csv "$(RESULTS_DIR)/$(MODE)"

plot: ## Plot both Locust modes for this run ID.
	$(UV) run --locked python plot_locust_results.py --input "$(RESULTS_DIR)" --output "$(RESULTS_DIR)/plots"

down: ## Stop containers and keep Pulp data.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down

status: ## Show container status.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml ps

destroy: ## Stop containers and remove Pulp volumes.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down --volumes --remove-orphans
