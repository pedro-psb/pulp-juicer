# Version retention experiment

Compare repository update costs with two candidates running the same upstream pulpcore `3.123.1` and built-in `pulp_file`:

- `retain-one`: `retain_repo_versions=1`.
- `unlimited`: default retention (`null`).

The `sql-models` scenario seeds 500,000 repository members directly with SQL, without content files or a fixture service. Each candidate then submits ten alternating one-item removals and additions. The runner waits for Pulp tasks to finish and records their execution times; HTTP response times only measure task submission.

Compare the `pulpcore.app.tasks.repository.add_and_remove` rows in the task-type duration table. The summary's PostgreSQL CPU statistics cover the request-submission window, which ends before the queued repository tasks drain.

```bash
source .venv/bin/activate
pulp-juicer squeeze version-retention sql-models --run
```

The default order runs each candidate once. Use `-s N` for N samples per candidate.
