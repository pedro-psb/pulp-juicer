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
UPSTREAM_URL=http://fixture:8000/ ./setup_pulp.sh

MODE=normal uv run --locked locust -f locustfile.py --headless -u 10 -r 10 -t 5m --csv normal
MODE=staging uv run --locked locust -f locustfile.py --headless -u 10 -r 10 -t 5m --csv staging
```

`make up` generates `fixture/` if needed and builds Pulp from the selected checkouts.
Run it again after editing either checkout.
`setup_pulp.sh` creates both repositories and distributions and writes their URLs to `setup.json`.
Pulp defaults to `http://localhost:5001` with `admin:password`.

Run one Locust mode at a time in a single process.
Each process requests its artifacts once and checks HTTP 200 and SHA256.
Response latency excludes the later commit.
The autocommit delay defaults to 30 seconds and the scan interval to 5 seconds; set `STAGING_DELAY_SECONDS` and `STAGING_SCAN_SECONDS` before `make up` to change them.

Check staging and committed versions after the run:

```bash
repo=$(jq -r '.staging.repository' setup.json)
curl -u admin:password "http://localhost:5001${repo}staging/" | jq '{count,results}'
curl -u admin:password "http://localhost:5001${repo}versions/" | jq '{count,results:[.results[].number]}'
```

Use a new run ID and fixture for each repeat so requests stay cold.
To regenerate, remove `fixture/` and run `make fixture RUN_ID=run002`.
`make down` keeps Pulp data; `make destroy` removes its named volumes but keeps the fixture and source checkouts.
