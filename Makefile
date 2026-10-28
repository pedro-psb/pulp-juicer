COMPOSE ?= docker compose
UV ?= uv
PROJECT ?= bucket-loadtest
PULPCORE_REPO ?= ../pulpcore
PULP_MAVEN_REPO ?= ../pulp_maven
RUN_ID ?= local001
COUNT ?= 1000
PAYLOAD_BYTES ?= 65536

export PULPCORE_REPO PULP_MAVEN_REPO

.DEFAULT_GOAL := help
.PHONY: help up down status destroy check-sources fixture

help: ## Show available targets and overrides.
	@printf 'Usage: make <target> [VARIABLE=value ...]\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-16s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nOverrides: COMPOSE, UV, PROJECT, PULPCORE_REPO, PULP_MAVEN_REPO, RUN_ID, COUNT, PAYLOAD_BYTES\n'

check-sources:
	$(UV) run --locked python check_sources.py --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)"

fixture: ## Generate Maven fixture artifacts.
	$(UV) run --locked python make_fixture.py --output fixture --run-id "$(RUN_ID)" --count "$(COUNT)" --payload-bytes "$(PAYLOAD_BYTES)"

up: check-sources ## Build and start Pulp and the fixture server.
	@test -f fixture/manifest.json || $(MAKE) fixture RUN_ID="$(RUN_ID)" COUNT="$(COUNT)" PAYLOAD_BYTES="$(PAYLOAD_BYTES)"
	$(COMPOSE) -p $(PROJECT) -f compose.yaml up -d --build

down: ## Stop containers and keep Pulp data.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down

status: ## Show container status.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml ps

destroy: ## Stop containers and remove Pulp volumes.
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down --volumes --remove-orphans
