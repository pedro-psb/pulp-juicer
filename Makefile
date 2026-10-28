COMPOSE ?= docker compose
PROJECT ?= bucket-loadtest
PULPCORE_REPO ?= ../pulpcore
PULP_MAVEN_REPO ?= ../pulp_maven
RUN_ID ?= local001
COUNT ?= 1000
PAYLOAD_BYTES ?= 65536

export PULPCORE_REPO PULP_MAVEN_REPO

.PHONY: up down status destroy check-sources fixture

check-sources:
	python check_sources.py --pulpcore "$(PULPCORE_REPO)" --maven "$(PULP_MAVEN_REPO)"

fixture:
	python make_fixture.py --output fixture --run-id "$(RUN_ID)" --count "$(COUNT)" --payload-bytes "$(PAYLOAD_BYTES)"

up: check-sources
	@test -f fixture/manifest.json || $(MAKE) fixture RUN_ID="$(RUN_ID)" COUNT="$(COUNT)" PAYLOAD_BYTES="$(PAYLOAD_BYTES)"
	$(COMPOSE) -p $(PROJECT) -f compose.yaml up -d --build

down:
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down

status:
	$(COMPOSE) -p $(PROJECT) -f compose.yaml ps

destroy:
	$(COMPOSE) -p $(PROJECT) -f compose.yaml down --volumes --remove-orphans
