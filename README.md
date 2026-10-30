# pulp-juicer

`pulp-juicer` is a Python package and CLI for declaring, planning, and running Pulp load-test experiments. Experiment definitions live under `src/experiments/`; the package builds their fixtures, manages Compose services, runs Locust measurements, records results, and renders reports.

## Setup

Install [uv](https://docs.astral.sh/uv/) and Docker Compose. Provide a pulpcore checkout at `../pulpcore`; candidate plugin sources are configured in each experiment. Then install the package and report dependencies:

```bash
make setup
```

## Use

```bash
make ls
uv run --locked pulp-juicer show maven-pull-through
uv run --locked pulp-juicer run maven-pull-through
uv run --locked pulp-juicer run maven-pull-through --scenario burst --run-id run001
uv run --locked pulp-juicer run maven-pull-through --all
uv run --locked pulp-juicer report
```

`run` without a scenario lists the available runs and prepares a plan under `_juice/`. Select one unit with `--scenario` and `--run-id`, run a scenario by omitting `--run-id`, or run the entire plan with `--all`. Existing results are skipped; use `-f` to replace them. Results and generated fixtures are stored under `_juice/`.

The Maven experiment and its source settings are documented in [its README](src/experiments/maven_pull_through/README.md). Set `PULPCORE_REPO` to use another pulpcore checkout. Set `COMPOSE` to override the default `docker compose` command. `uv run --locked pulp-juicer ps` lists active experiment services; `uv run --locked pulp-juicer clean` removes their containers and networks.

## Development

```bash
make format
make lint
make test
```

See [architecture](docs/architecture.md) for the package design.
