# Maven pull-through load test

Compare cold pull-through requests against Maven repositories with staging and autocommit enabled or disabled.
Each candidate gets unique artifact paths and bytes.

## Prerequisites

Install Docker Compose with named build context support, [uv](https://docs.astral.sh/uv/), `curl`, and `jq`.
uv selects Python 3.12 and installs `packaging` and Locust from the committed lockfile.
`make report` also installs the `quarto-cli` Python package through uv; its first installation downloads the Quarto binary from GitHub.

Provide compatible pulpcore and pulp_maven sources.
The default paths are local checkouts at `../pulpcore` and `../pulp_maven`.
Relative source paths are resolved from this repository root.
Set `PULPCORE_REPO` and `PULP_MAVEN_REPO` to another local checkout or a Git URL, for example:

```bash
PULP_MAVEN_REPO='https://github.com/pulp/pulp_maven.git#your-ref' make up RUN_ID=run002
```

Pin a full commit hash for repeatable runs.
Local checkouts include uncommitted changes; Git URLs use committed source.
Compatibility is checked before the build for local sources and during the image build when either source is remote.
The staging comparison requires a Maven source whose pull-through hook honors `staging_enabled`.
The image follows the [pulp-oci-images custom image guide](https://pulpproject.org/pulp-oci-images/docs/admin/guides/build-your-own-pulp-image/).

## Run

Run `make` to see available targets and overrides.
From this directory, set a run ID and run both candidates:

```bash
export RUN_ID=run001
make run-all COUNT=1000
make plot
```

`make run-all` calls `make up`, runs normal then staging, and calls `make report`.
Run each target separately when needed.

`make up` generates the shared `fixture/` if needed, builds and starts Pulp, then creates both repositories and distributions.
After editing either checkout, use `make destroy` and a new run ID before running `make up` again.
BuildKit reuses its `uv` cache across regular rebuilds; `--no-cache` discards it.
Setup writes the distribution URLs to `_results/_run/$RUN_ID/setup.json`.
It records fixture details and Pulp source commits or refs in `_results/_run/$RUN_ID/setup_metadata.json`; `make run` needs both files for that run ID.
It waits up to five minutes for Pulp's database, workers, and content app; set `PULP_READY_TIMEOUT` to change the limit.
`make run` writes Locust, per-request, and PostgreSQL CPU CSV files under `_results/$CANDIDATE/$RUN_ID/`.
It also saves `run_metadata.json` with the effective fixture COUNT, configured concurrency and timing, Pulp source commits or refs, and setup details, plus copies of the fixture manifest and setup file.
`requests.csv` records each artifact path, request start and completion times in UTC, HTTP response time, status, and outcome.
`make run` records five idle seconds before requests and five after they stop in Locust history and PostgreSQL CPU samples.
Set `IDLE_SECONDS` and `TAIL_SECONDS` to change these periods.
`DURATION` is Locust's time limit and includes the initial idle period; the tail runs after the test stops.
PostgreSQL CPU is sampled once per second in `db_cpu.csv`, and `db_cpu_summary.csv` covers the full run.
The Quarto report overlays one-second request-start counts on the completed-requests chart and includes the CPU summary and other timeline charts.
CPU percent is summed across PostgreSQL processes, so 100% means one fully used CPU core.
Comparison plots are written under `_results/_comparison/$RUN_ID/`.
`make report` renders one `_results/report.html` with a tab for each completed candidate run and an overview table.
Each run tab groups metrics and run configuration under Summary, shows charts under Timeline, and links the source files used for the run.
The completed-requests chart overlays active users; throughput and PostgreSQL CPU charts overlay cumulative completed requests on the right axis.
It scans all run IDs, so `RUN_ID` is not required.
It also writes `_results/report.qmd`; SVG charts remain in each run directory.
`QUARTO=/path/to/quarto` can override the packaged CLI.
`COUNT`, `PAYLOAD_BYTES`, and `FIXTURE_ID` apply when `fixture/` is first generated.
Use `make run CANDIDATE=normal USERS=10 SPAWN_RATE=10 DURATION=5m WAIT_SECONDS=0` to adjust the load.
Pulp defaults to `http://localhost:5001` with `admin:password`.
The container defaults to one API, one content, and two task workers; override with `PULP_API_WORKERS`, `PULP_CONTENT_WORKERS`, and `PULP_WORKERS` before `make up`.

Run one Locust candidate at a time in a single process.
Each process requests its artifacts once and checks HTTP 200 and SHA256.
Locust exits after the last request completes; `DURATION` remains a time limit for incomplete runs.
Response latency excludes the later commit.
The autocommit delay defaults to 30 seconds and the scan interval to 5 seconds; set `STAGING_DELAY_SECONDS` and `STAGING_SCAN_SECONDS` before `make up` to change them.

Check staging and committed versions after the run:

```bash
repo=$(jq -r '.staging.repository' "_results/_run/$RUN_ID/setup.json")
curl -u admin:password "http://localhost:5001${repo}staging/" | jq '{count,results}'
curl -u admin:password "http://localhost:5001${repo}versions/" | jq '{count,results:[.results[].number]}'
```

Reset Pulp with `make destroy` before repeating the test with a new run ID so the shared artifact paths stay cold.
If you open a new shell, set the same `RUN_ID` again before running the next candidate.
`make destroy` stops Pulp and removes its named volumes but keeps `fixture/`, `_results/`, and source checkouts.
`make clean` removes `_results/` while keeping the shared fixture and Pulp volumes.

## How it works

Normal pull-through adds each artifact to a repository version; staging collects artifacts until autocommit creates a version.

- `Makefile`, `compose.yaml`, `Containerfile`, and `settings/` build and run Pulp and the fixture server.
- `make_fixture.py` writes Maven artifacts and a manifest to the shared `fixture/`.
- `setup_pulp.sh` creates the remote, both repositories, and their distributions, then writes `_results/_run/$RUN_ID/setup.json`.
- `locustfile.py` reads the manifest and setup file, requests each artifact once, and checks its checksum.
- `plot_locust_results.py` writes comparison plots to `_results/_comparison/$RUN_ID/`.
- `generate_reports.py` builds a combined Quarto report from Locust and PostgreSQL CPU CSV files.
- `pyproject.toml` and `uv.lock` define the local Python tools.
