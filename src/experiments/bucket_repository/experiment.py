"""Bucket repository experiment definition."""

from pathlib import Path

from pulp_juicer import (
    CandidateConfig,
    ExperimentConfig,
    LoadStep,
    PulpPrepare,
    RequireFixture,
    ScenarioConfig,
    StepFunctionLoad,
    UnitaryImpulseLoad,
    sample_sequence,
)

from . import scenario_input
from .candidates import NormalCandidate, StagingCandidate
from .data_service.fixture import MavenServiceFixture

# Variable keys come from summary_statistics.VARIABLES; trial_phases selects a transposed table.
SELECTED_STATS = (
    "trial_phases",
    "request_failure",
    "request_completions",
    "request_latency",
    "task_dispatches",
    "task_execution",
    "pg_cpu",
    "pg_pss",
    "pg_cpu_seconds",
)

EXPERIMENT = ExperimentConfig(
    id="bucket-repository",
    short_description="Compare standard and bucket repositories under Maven load.",
    description=(
        "Compare Maven pull-through and upload behavior for standard and bucket repositories. "
        "Both workloads use the same generated artifact fixture, allowing response time "
        "and request completion to be compared across repeated runs."
    ),
    required_sources=("pulpcore", "pulp_maven"),
    fixtures=(
        MavenServiceFixture(
            name="maven",
            output_dir=Path("fixture/maven"),
        ),
    ),
    candidates=(
        CandidateConfig(
            id=1,
            name="normal",
            description="Released Pulp sources with standard Maven repository behavior.",
            sources={
                "pulpcore": "https://github.com/pulp/pulpcore.git#3.123.1",
                "pulp_maven": "https://github.com/pulp/pulp_maven.git#0.37.0",
            },
        ),
        CandidateConfig(
            id=2,
            name="staging",
            description="Bucket repository implementation with repository staging area concept.",
            sources={
                "pulpcore": "../pulpcore",
                "pulp_maven": "../pulp_maven",
            },
        ),
    ),
    scenarios=(
        ScenarioConfig(
            id="pull-through-1k-impulse",
            description="Request 1,000 unique Maven artifacts in one impulse.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=sample_sequence(n=3, candidates=(1, 2)),
            load=UnitaryImpulseLoad(request_count=1000),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository with autocommit",
                    settings={
                        "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                        "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                    },
                    pre_test_fn=StagingCandidate.setup(autocommit=True),
                ),
            },
        ),
        ScenarioConfig(
            id="pull-through-1k-impulse-repeated-requests",
            description="Request the same 100 Maven artifacts ten times in one impulse.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through_repeated,
            run_order=sample_sequence(n=3, candidates=(1, 2)),
            load=UnitaryImpulseLoad(request_count=1000),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository with autocommit",
                    settings={
                        "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                        "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                    },
                    pre_test_fn=StagingCandidate.setup(autocommit=True),
                ),
            },
        ),
        ScenarioConfig(
            id="pull-through-step-30-users",
            description="Thirty concurrent users request 650 unique Maven artifacts.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=sample_sequence(n=3, candidates=(1, 2)),
            load=StepFunctionLoad(
                steps=(LoadStep(at_seconds=0, users=30),),
                request_count=650,
            ),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository with autocommit",
                    settings={
                        "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                        "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                    },
                    pre_test_fn=StagingCandidate.setup(autocommit=True),
                ),
            },
        ),
        ScenarioConfig(
            id="pull-through-step-30-users-unique-repeated",
            description=(
                "Thirty users request 12,000 unique Maven artifacts, then repeat "
                "the same set once."
            ),
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 15000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through_unique_then_repeated,
            run_order=sample_sequence(n=1, candidates=(1, 2)),
            load=StepFunctionLoad(
                steps=(LoadStep(at_seconds=0, users=30),),
                request_count=scenario_input.LONG_STEP_UNIQUE_COUNT
                * (1 + scenario_input.LONG_STEP_REPEAT_ROUNDS),
                max_duration="30m",
            ),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository with autocommit",
                    settings={
                        "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                        "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                    },
                    pre_test_fn=StagingCandidate.setup(autocommit=True),
                ),
            },
        ),
        ScenarioConfig(
            id="upload-impulse",
            description="Upload/Add 100 unique Maven JARs to a repository.",
            driver="http_upload",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_upload,
            run_order=sample_sequence(n=3, candidates=(1, 2)),
            load=UnitaryImpulseLoad(request_count=100),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository without autocommit",
                    settings={},
                    pre_test_fn=StagingCandidate.setup(autocommit=False),
                ),
            },
        ),
        ScenarioConfig(
            id="tmp",
            description="Temporary experiments.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=(1, 2),
            draft=True,
            load=StepFunctionLoad(
                steps=(LoadStep(at_seconds=0, users=30),),
                request_count=120,
            ),
            selected_stats=SELECTED_STATS,
            candidate_config={
                "normal": PulpPrepare(
                    description="Create Maven remote, repository, and distribution",
                    settings={},
                    pre_test_fn=NormalCandidate.setup(),
                ),
                "staging": PulpPrepare(
                    description="Create staging Maven repository with autocommit",
                    settings={
                        "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                        "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
                    },
                    pre_test_fn=StagingCandidate.setup(autocommit=True),
                ),
            },
        ),
    ),
)
