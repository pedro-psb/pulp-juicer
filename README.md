# pulp-juicer

Run Pulp load-test experiments and compare candidates through generated reports.

## Install

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker Compose.
The staging candidate uses sibling checkouts at `../pulpcore` and `../pulp_maven`.

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e . --group report
```

## Run an experiment

```bash
pulp-juicer squeeze bucket-repository
pulp-juicer squeeze bucket-repository pull-through-1k-impulse
pulp-juicer squeeze bucket-repository pull-through-1k-impulse --run
```

The first two commands show the available candidates and scenarios without running them.
Add `-s N` to a `--run` command to use N samples per candidate, or name a candidate before `--run` for one ad-hoc measurement.
Run `pulp-juicer squeeze --help` for all options.

Executions write data and a ZIP archive under `_juice/executions/`.
Completed measurements also produce HTML reports.
To find or regenerate a report:

```bash
pulp-juicer report
pulp-juicer report bucket-repository__pull-through-1k-impulse__001 --archive
```

See the [bucket repository experiment](src/experiments/bucket_repository/README.md) for candidate and scenario details.
The [AI-oriented architecture reference](docs/architecture.md) contains implementation context for coding agents.
