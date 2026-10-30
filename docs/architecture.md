# Architecture

```text
experiments/* -> ls -> show <experiment>
              -> run -> _juice/plan-<experiment>.json (created if absent)
                         runtime + scenarios[unit runs]
                                          |
run <experiment> [--all|--scenario|--run-id] -> selected runs
                                          |
                              Compose cleanup -> Locust library
                              test_start: fixture, Pulp, candidate setup
                              test_stop: CPU summary, Compose down
                                          |
                              _juice/results + RunResultMetadata
```

The plan is the global run. Each ordered scenario contains its unit runs; each unit records `(experiment, scenario, run_id)` plus candidate, fixture, and load metadata. `r` marks a cold Pulp restart. Generated fixtures and results live under `_juice/`.

| Scope | Command |
|:--|:--|
| List available runs | `pulp-juicer run maven-pull-through` |
| Experiment | `pulp-juicer run maven-pull-through --all` |
| Scenario | `pulp-juicer run maven-pull-through --scenario burst` |
| Unit | `pulp-juicer run maven-pull-through --scenario burst --run-id run001` |

`run` creates the plan on first use and loads it from `_juice/` afterward; `--refresh-plan` rebuilds it. `ls` lists experiments and scenarios with short descriptions; `show <experiment>` shows its full description, scenarios, candidates, and runs. `ps` lists pulp-juicer services, and `clean` removes their containers and networks. Existing unit results are skipped unless `run -f` is used; unsuccessful unit results are removed.

Each successful run stores configuration, setup, source details, and result metrics in one `run_metadata.json`. Its selected requests are saved as `data_load.csv`; the full manifest stays in the shared fixture directory.
