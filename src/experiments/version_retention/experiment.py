"""Compare eager retention with unlimited versions after SQL model seeding."""

from pulp_juicer import (
    CandidateConfig,
    ExperimentConfig,
    LoadStep,
    PreTestFunction,
    ScenarioConfig,
    StepFunctionLoad,
)

from .scenario import (
    change_one,
    create_repository_retain_one,
    create_repository_unlimited,
    seed_sql,
)

LOAD = StepFunctionLoad(
    steps=(LoadStep(at_seconds=0, users=1),),
    request_count=10,
    max_duration="30m",
    request_timeout_seconds=120,
)

EXPERIMENT = ExperimentConfig(
    id="version-retention",
    short_description="Compare eager and unlimited version retention after a 500k SQL seed.",
    description="Compare ten one-item repository changes with retain_repo_versions=1 and default unlimited retention after seeding 500,000 members directly through SQL.",
    required_sources=("pulpcore",),
    fixtures=(),
    candidates=(
        CandidateConfig(
            id=1,
            name="retain-one",
            description="Upstream pulpcore 3.123.1; retain_repo_versions=1.",
            sources={"pulpcore": "https://github.com/pulp/pulpcore.git#3.123.1"},
            pre_test_functions=[
                PreTestFunction(
                    description="Create file repository with retain_repo_versions=1",
                    setup_fn=create_repository_retain_one,
                )
            ],
        ),
        CandidateConfig(
            id=2,
            name="unlimited",
            description="Upstream pulpcore 3.123.1; default unlimited version retention.",
            sources={"pulpcore": "https://github.com/pulp/pulpcore.git#3.123.1"},
            pre_test_functions=[
                PreTestFunction(
                    description="Create file repository with default version retention",
                    setup_fn=create_repository_unlimited,
                )
            ],
        ),
    ),
    scenarios=(
        ScenarioConfig(
            id="sql-models",
            description="Seed 500,000 file members with SQL, then make ten one-item changes.",
            driver="http_post",
            input=change_one,
            run_order=(1, 2),
            load=LOAD,
            setup_fn=seed_sql,
            selected_stats=("task_execution", "pg_cpu", "pg_cpu_seconds"),
        ),
    ),
)
