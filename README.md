# Pull-through load test starter

This runs the same cold pull-through workload against two Maven repositories: one with staging and autocommit enabled, and one with staging disabled. Each request uses a different artifact path. The fixture contains separate, equivalent artifacts for the two modes, so one mode cannot warm the other's cache.

## Requirements

- Docker Compose with named build context support
- Local Git checkouts of this pulpcore branch and a compatible `pulp_maven` checkout
- Python 3 and [Locust](https://docs.locust.io/en/stable/installation.html) on the load generator
- `curl` and `jq` for setup
- Python's `packaging` module for the source compatibility check

The local `pulp` CLI in this environment has no Maven command, so `setup_pulp.sh` uses the Maven REST endpoints through `curl`.
The image follows the [pulp-oci-images custom image guide](https://pulpproject.org/pulp-oci-images/docs/admin/guides/build-your-own-pulp-image/): it derives from `pulp/pulp-ci-centos10`, installs local source into the image, and collects static files. Compose also serves the fixture to Pulp.

The default sources are the sibling Git worktrees `../pulpcore` and `../pulp_maven`. The Maven worktree was rebased onto upstream/main and is compatible with this pulpcore branch. Override `PULPCORE_REPO` or `PULP_MAVEN_REPO` with another local checkout if needed; paths may be absolute or relative to this directory. `make up` checks declared version compatibility before building.

## Run

From this directory, create the fixture and start Pulp:

```bash
make up RUN_ID=run001 COUNT=1000
make status
```

`make up` generates `fixture/` if it does not exist. The container image contains a snapshot of the source checkouts at build time; run `make up` again after editing them. The fixture is available to Pulp at `http://fixture:8000/`. Create the Pulp resources:

```bash
export RUN_ID=run001
export UPSTREAM_URL=http://fixture:8000/
export PULP_BASE_URL=http://localhost:5001
./setup_pulp.sh
```

The setup writes `setup.json` with repository hrefs and distribution URLs. It creates new resources; it does not delete or reuse existing ones. The default local credentials are `admin:password`. The Compose settings use a 30-second autocommit delay and a 5-second scan interval; both can be overridden with `STAGING_DELAY_SECONDS` and `STAGING_SCAN_SECONDS` when starting Pulp.

Run one mode at a time:

```bash
MODE=normal locust -f locustfile.py --headless -u 10 -r 10 -t 5m --csv normal
MODE=staging locust -f locustfile.py --headless -u 10 -r 10 -t 5m --csv staging
```

Set `CONTENT_ORIGIN=http://localhost:5002` if the distribution's advertised host is not reachable from the load generator. The script preserves the distribution path. Override `SETUP_JSON`, `MANIFEST_JSON`, or `WAIT_SECONDS` as needed. Each Locust process consumes its mode's manifest once, then stops requesting artifacts. Use a single Locust process; the in-memory index is not shared across distributed workers.

The Locust request is successful only if the full response has HTTP 200 and matches the fixture's SHA256. Its latency covers the content response, not the later autocommit. Check the staging list and repository versions after the run. If staging stays empty while the normal repository gains versions, verify that the installed `pulp_maven` and running pulpcore revision route pull-through content into staging.

For a quick inspection:

```bash
repo=$(jq -r '.staging.repository' setup.json)
curl -u admin:password "${PULP_BASE_URL}${repo}staging/" | jq '{count,results}'
curl -u admin:password "${PULP_BASE_URL}${repo}versions/" | jq '{count,results:[.results[].number]}'
```

Keep Pulp settings, workers, storage, and fixture server unchanged between modes. Recreate the fixture and Pulp resources with a new run ID for every repeat so requests remain cold.

`make down` stops the containers and keeps Pulp data. `make destroy` removes the containers and their named Pulp volumes. It preserves the fixture and local Git checkouts. To generate a new fixture, remove `fixture/` and run `make fixture RUN_ID=run002` before the next `make up`.
