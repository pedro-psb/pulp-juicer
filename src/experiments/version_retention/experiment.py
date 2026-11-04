"""Compare real file sync and SQL model seeding for retention work."""

from pathlib import Path

from pulp_juicer import (
    CandidateConfig,
    ExperimentConfig,
    LoadStep,
    RequireFixture,
    ScenarioConfig,
    StepFunctionLoad,
)

from .fixture import FileFixture
from .scenario import change_one, seed_file_sync, seed_sql

LOAD = StepFunctionLoad(
    steps=(LoadStep(at_seconds=0, users=1),),
    request_count=10,
    max_duration="30m",
    request_timeout_seconds=120,
)

EXPERIMENT = ExperimentConfig(
    id="version-retention",
    short_description="Measure eager retention of a 500k member file repository.",
    description="Ten small repository changes after a 500k member seed, using real file sync or SQL model insertion.",
    required_sources=("pulpcore",),
    fixtures=(FileFixture("files", Path("fixture/files")),),
    candidates=(
        CandidateConfig(
            id=1,
            name="upstream",
            description="Upstream pulpcore 3.123.1 with built-in pulp_file.",
            sources={"pulpcore": "https://github.com/pulp/pulpcore.git#3.123.1"},
            pre_test_functions=[],
        ),
    ),
    scenarios=(
        ScenarioConfig(
            id="file-sync",
            description="Seed via pulp_file manifest sync.",
            driver="http_post",
            input=change_one,
            run_order=(1,),
            load=LOAD,
            requires=(RequireFixture("files"),),
            setup_fn=seed_file_sync,
            selected_stats=("task_execution", "pg_cpu", "pg_cpu_seconds"),
        ),
        ScenarioConfig(
            id="sql-models",
            description="Seed model rows with set based SQL.",
            driver="http_post",
            input=change_one,
            run_order=(1,),
            load=LOAD,
            setup_fn=seed_sql,
            selected_stats=("task_execution", "pg_cpu", "pg_cpu_seconds"),
        ),
    ),
)
