# Maven pull-through load test

Compare cold pull-through requests against Maven repositories with staging and autocommit enabled or disabled.
Each scenario gets unique artifact paths and bytes.

## Prerequisites

Install Docker Compose with named build context support, [uv](https://docs.astral.sh/uv/), `curl`, and `jq`.
uv selects Python 3.12 and installs `packaging` and Locust from the committed lockfile.

Provide compatible local Git checkouts of pulpcore and pulp_maven.
The default paths are `../pulpcore` and `../pulp_maven`; set `PULPCORE_REPO` and `PULP_MAVEN_REPO` if yours are elsewhere.
The staging comparison requires a Maven checkout whose pull-through hook honors `staging_enabled`.
The image follows the [pulp-oci-images custom image guide](https://pulpproject.org/pulp-oci-images/docs/admin/guides/build-your-own-pulp-image/).

## Run

Run `make` to see available targets and overrides.
From this directory, set a run ID and start the services:

```bash
export RUN_ID=run001
make up COUNT=1000
make status
make setup
make run SCENARIO=normal
make run SCENARIO=staging
make plot
```

`make up` generates the shared `fixture/` if needed and builds Pulp from the selected checkouts.
Run it again after editing either checkout.
BuildKit reuses its `uv` cache across regular rebuilds; `--no-cache` discards it.
`make setup` creates both repositories and distributions and writes their URLs to `_results/_run/$RUN_ID/setup.json`.
It waits up to five minutes for Pulp's database, workers, and content app; set `PULP_READY_TIMEOUT` to change the limit.
Locust CSV files and a charted `report.html` are written under `_results/$SCENARIO/$RUN_ID/`.
`make run` also records PostgreSQL CPU once per second in `db_cpu.csv` and writes `db_cpu_summary.csv` when Locust stops.
CPU percent is summed across PostgreSQL processes, so 100% means one fully used CPU core.
Comparison plots are written under `_results/_comparison/$RUN_ID/`.
`COUNT`, `PAYLOAD_BYTES`, and `FIXTURE_ID` apply when `fixture/` is first generated.
Use `make run SCENARIO=normal USERS=10 SPAWN_RATE=10 DURATION=5m WAIT_SECONDS=0` to adjust the load.
Pulp defaults to `http://localhost:5001` with `admin:password`.

Run one Locust scenario at a time in a single process.
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
If you open a new shell, set the same `RUN_ID` again before running the next scenario.
The earlier root-level `setup.json` belongs to the old layout.
`make down` keeps Pulp data; `make destroy` removes its named volumes but keeps `fixture/`, `_results/`, and source checkouts.
`make clean` removes `_results/` while keeping the shared fixture and Pulp volumes.

## How it works

Normal pull-through adds each artifact to a repository version; staging collects artifacts until autocommit creates a version.

- `Makefile`, `compose.yaml`, `Containerfile`, and `settings/` build and run Pulp and the fixture server.
- `make_fixture.py` writes Maven artifacts and a manifest to the shared `fixture/`.
- `setup_pulp.sh` creates the remote, both repositories, and their distributions, then writes `_results/_run/$RUN_ID/setup.json`.
- `locustfile.py` reads the manifest and setup file, requests each artifact once, and checks its checksum.
- `plot_locust_results.py` writes comparison plots to `_results/_comparison/$RUN_ID/`.
- `pyproject.toml` and `uv.lock` define the local Python tools.
