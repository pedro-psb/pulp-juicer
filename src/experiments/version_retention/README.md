# Version retention experiment

Using upstream pulpcore `3.123.1` and built-in `pulp_file`, both scenarios seed 500,000 repository members with `retain_repo_versions=1`, then measure ten one-item changes.

- `file-sync` seeds through a manifest and real files.
- `sql-models` seeds directly with SQL, without content files.

Run `pulp-juicer squeeze version-retention file-sync upstream --run`, replacing `file-sync` with `sql-models` for the other scenario. Seeding happens before measurement.
