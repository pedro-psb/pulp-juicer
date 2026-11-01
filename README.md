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
uv run --locked pulp-juicer squeeze
uv run --locked pulp-juicer show maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through
uv run --locked pulp-juicer squeeze maven-pull-through --scenario burst --run-id run001
uv run --locked pulp-juicer squeeze maven-pull-through --all
uv run --locked pulp-juicer report
uv run --locked pulp-juicer report maven-pull-through__short-impulse__018
uv run --locked pulp-juicer results maven-pull-through
```

`squeeze` without an experiment lists available experiments and shows usage. With an experiment, it lists that experiment's scenarios and runs. Select one run with `--scenario` and `--run-id`, a scenario by omitting `--run-id`, or the entire experiment with `--all`. Each squeeze gets a numbered execution ID and a result directory under `_juice/`, including `execution.log` for process events. The plan runs in memory; add `--save-plan` to write its JSON file. Use `-f` to replace the latest execution for the selected scope. Generated fixtures are also stored under `_juice/`. Before teardown, a collector saves Pulp database tasks to `collectors/tasking.parquet`; Compose logs remain available for debugging. `report` creates a detailed `report.html` with task analysis plus a run-level `trials.html` in each execution directory. Pass an execution ID to report only that execution; omit it to report all executions. `results` prints the experiment's run directories and generated report files.

For one run, add `--post-debug` to keep its Compose services running after the test. Squeeze prints `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE` exports followed by commands to inspect the service, open a shell, query Pulp, follow logs, and remove the project when finished. For example: `pulp-juicer squeeze maven-pull-through --scenario burst --run-id run001 --post-debug`.

The Maven experiment and its source settings are documented in [its README](src/experiments/maven_pull_through/README.md). Each candidate supplies the pulpcore and plugin sources required by the experiment. Set `COMPOSE` to override the default `docker compose` command. `uv run --locked pulp-juicer ps` lists active experiment services. Use `pulp-juicer clean services`, `clean results`, `clean fixtures`, or `clean all`; selections such as `clean results,fixtures` are also supported. `all` keeps saved plan files.

## Development

```bash
make format
make lint
make test
```

See [architecture](docs/architecture.md) for the package design.
