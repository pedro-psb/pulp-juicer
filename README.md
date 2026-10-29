# Maven pull-through load test

Compare cold pull-through requests against Maven repositories with staging and autocommit enabled or disabled.
Each mode gets unique artifact paths and bytes.

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
make run MODE=normal
make run MODE=staging
make plot
```

`make up` generates `_results/$RUN_ID/fixture/` if needed and builds Pulp from the selected checkouts.
Run it again after editing either checkout.
`make setup` creates both repositories and distributions and writes their URLs to `_results/$RUN_ID/setup.json`.
Locust CSV files and plots are written under the same run directory.
Use `make run MODE=normal USERS=10 SPAWN_RATE=10 DURATION=5m WAIT_SECONDS=0` to adjust the load.
Pulp defaults to `http://localhost:5001` with `admin:password`.

Run one Locust mode at a time in a single process.
Each process requests its artifacts once and checks HTTP 200 and SHA256.
Locust exits after the last request completes; `DURATION` remains a time limit for incomplete runs.
Response latency excludes the later commit.
The autocommit delay defaults to 30 seconds and the scan interval to 5 seconds; set `STAGING_DELAY_SECONDS` and `STAGING_SCAN_SECONDS` before `make up` to change them.

Check staging and committed versions after the run:

```bash
repo=$(jq -r '.staging.repository' "_results/$RUN_ID/setup.json")
curl -u admin:password "http://localhost:5001${repo}staging/" | jq '{count,results}'
curl -u admin:password "http://localhost:5001${repo}versions/" | jq '{count,results:[.results[].number]}'
```

Use a new run ID for each repeat so requests stay cold.
`make down` keeps Pulp data; `make destroy` removes its named volumes but keeps `_results/` and source checkouts.

## How it works

Normal pull-through adds each artifact to a repository version; staging collects artifacts until autocommit creates a version.

- `Makefile`, `compose.yaml`, `Containerfile`, and `settings/` build and run Pulp and the fixture server.
- `make_fixture.py` writes Maven artifacts and a manifest to `_results/$RUN_ID/fixture/`.
- `setup_pulp.sh` creates the remote, both repositories, and their distributions, then writes `_results/$RUN_ID/setup.json`.
- `locustfile.py` reads the manifest and setup file, requests each artifact once, and checks its checksum.
- `plot_locust_results.py` writes comparison plots to `_results/$RUN_ID/plots/`.
- `pyproject.toml` and `uv.lock` define the local Python tools.
