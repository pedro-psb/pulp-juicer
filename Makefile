UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help setup format lint test ls

help: ## Show available targets.
	@printf 'Usage: make <target>\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-10s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

setup: ## Install the editable package and report dependencies.
	$(UV) venv
	$(UV) pip install -e . --group report

format: ## Format Python files with Ruff.
	$(UV) run --no-sync --with ruff ruff format src tests
	$(UV) run --no-sync --with ruff ruff check --fix src tests

lint: ## Lint Python files with Ruff.
	$(UV) run --no-sync --with ruff ruff check src tests

test: ## Run the test suite.
	$(UV) run --no-sync --with pytest pytest -p no:anyio tests

ls: ## List experiments and scenarios.
	$(UV) run --no-sync pulp-juicer ls
