"""Bucket repository experiment definition."""

from pathlib import Path

from pulp_juicer import (
    CandidateConfig,
    ExperimentConfig,
    LoadStep,
    PreTestFunction,
    RequireFixture,
    ScenarioConfig,
    sample_sequence,
    StepFunctionLoad,
    UnitaryImpulseLoad,
)

from . import candidates, scenario_input
from .data_service.fixture import MavenServiceFixture

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
            pre_test_functions=[
                PreTestFunction(
                    description="Create Maven remote, repository, and distribution",
                    setup_fn=candidates.setup_normal,
                )
            ],
        ),
        CandidateConfig(
            id=2,
            name="staging",
            description="Bucket repository implementation with repository staging area concept.",
            pulp_settings={
                "STAGING_AUTOCOMMIT_DELAY_SECONDS": 10,
                "STAGING_AUTOCOMMIT_SCAN_INTERVAL_SECONDS": 5,
            },
            sources={
                "pulpcore": "../pulpcore",
                "pulp_maven": "../pulp_maven",
            },
            pre_test_functions=[
                PreTestFunction(
                    description="Create staging Maven remote, repository, and distribution",
                    setup_fn=candidates.setup_staging,
                )
            ],
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
        ),
        ScenarioConfig(
            id="upload-impulse",
            description="Upload/Add 100 unique Maven JARs to a repository.",
            driver="http_upload",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_upload,
            run_order=sample_sequence(n=3, candidates=(1,2)),
            load=UnitaryImpulseLoad(request_count=100),
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
        ),
    ),
)
