UV ?= uv

.DEFAULT_GOAL := help
.PHONY: help setup format lint test ls

help: ## Show available targets.
	@printf 'Usage: make <target>\n\nTargets:\n'
	@awk 'BEGIN { FS = ":.*## " } /^[a-zA-Z0-9_-]+:.*## / { printf "  %-10s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

setup: ## Sync locked development and report dependencies.
	$(UV) sync --locked --group dev --group report

format: ## Format Python files with Ruff.
	$(UV) run --locked --group dev ruff format src tests
	$(UV) run --locked --group dev ruff check --fix src tests

lint: ## Lint Python files with Ruff.
	$(UV) run --locked --group dev ruff check src tests

test: ## Run the test suite.
	$(UV) run --locked --group dev pytest -p no:anyio tests

ls: ## List experiments and scenarios.
	$(UV) run --locked pulp-juicer ls
