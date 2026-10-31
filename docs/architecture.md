# Architecture

```text
experiments/* -> ls -> show <experiment>
              -> squeeze <experiment> -> available runs
              -> squeeze <experiment> [--all|--scenario|--run-id]
                                          |
                            in-memory plan: selected unit runs
                            optional --save-plan -> _juice/plan-<execution_id>.json
                                          |
                              Compose cleanup -> fixture, Pulp, candidate setup
                              Locust library: idle, load, tail, measurements
                              CPU summary -> Compose down
                                          |
                              _juice/results/<execution_id>/ + RunResultMetadata
```

Each `squeeze` call is an execution, identified by `experiment[__scenario[__run_id]]__NNN` (for example, `maven-pull-through__burst__001`). The final number increases across the workspace. Its plan contains the selected ordered runs; each run records `(experiment, scenario, run_id)` plus candidate, fixture, and load metadata. Each scenario's `run_order` lists candidate IDs, and Pulp is reset before every run. The experiment declares required source names, and each candidate supplies a source for every name. Generated fixtures and results live under `_juice/`.

| Scope | Command |
|:--|:--|
| List available runs | `pulp-juicer squeeze maven-pull-through` |
| Experiment | `pulp-juicer squeeze maven-pull-through --all` |
| Scenario | `pulp-juicer squeeze maven-pull-through --scenario burst` |
| Unit | `pulp-juicer squeeze maven-pull-through --scenario burst --run-id run001` |

Each squeeze builds its plan in memory and reserves a result directory. `_juice/.execution-sequence` keeps execution numbers increasing across calls. `--save-plan` writes the selected inputs as JSON, omitting the Pulp password. `-f` replaces the latest execution for the selected scope. `_juice/results/<execution_id>/execution.log` records plan preparation, fixture reuse, test hooks, load start and stop, and failures. `ls` lists experiments and scenarios with short descriptions; `show <experiment>` shows its full description, scenarios, candidates, and runs. `ps` lists pulp-juicer services. `clean <targets>` removes services, results, fixtures, or all three; targets can be comma separated, and saved plans remain. Completed runs retain results even when requests fail; incomplete runs retain only their Compose log.

Each successful run stores its execution ID, configuration, setup, source details, and result metrics in one `run_metadata.json`. Its selected requests are saved as `data_load.csv`; the full manifest stays under `_juice/fixture/`. After the load test and before service teardown, it saves the Pulp status API response as `pulp_status.json` in the unit result directory. If status capture fails, it saves `pulp_status_error.txt` and retains the completed run. Every run saves timestamped service output from Compose in `compose.log` before teardown, including failed runs; incomplete measurement files are removed while this log is kept.
`report` renders each execution's completed runs to `_juice/results/<execution_id>/report.html` and compares runs as trials in `trials.html`; it lists paths under `New Reports` and `Existing`. `results <experiment>` prints its existing run directories and their execution reports.
