"""Maven pull-through experiment definition."""

from pathlib import Path

from pulp_juicer import (
    CandidateConfig,
    ExperimentConfig,
    LoadConfig,
    PreTestFunction,
    RequireFixture,
    ScenarioConfig,
)

from . import candidates, scenario_input
from .data_service.fixture import MavenServiceFixture

EXPERIMENT = ExperimentConfig(
    id="maven-pull-through",
    short_description="Compare Maven pull-through candidates under two load patterns.",
    description=(
        "Compare Maven pull-through implementations under burst and moderate request loads. "
        "Each candidate serves artifacts from the same generated upstream fixture, allowing "
        "response time and request completion to be compared across repeated runs."
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
            id="calibrate",
            description="Analyse system response to a big impulse load",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=(1, 2, 2, 1, 2, 2, 1, 1),
            load=LoadConfig(
                users=1000,
                request_count=1000,
                spawn_rate_per_second=1000,
                max_duration="5m",
                wait_seconds=0,
                idle_before_seconds=5,
                tail_after_seconds=5,
                request_timeout_seconds=60,
            ),
        ),
        ScenarioConfig(
            id="burst",
            description="Thirty concurrent users request 650 unique Maven artifacts.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=(1, 1, 2, 2),
            load=LoadConfig(
                users=30,
                request_count=650,
                spawn_rate_per_second=30,
                max_duration="5m",
                wait_seconds=0,
                idle_before_seconds=5,
                tail_after_seconds=5,
                request_timeout_seconds=60,
            ),
        ),
        ScenarioConfig(
            id="tmp",
            description="Temporary experiments.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            run_order=(1,),
            load=LoadConfig(
                users=10,
                request_count=120,
                spawn_rate_per_second=10,
                max_duration="5m",
                wait_seconds=0,
                idle_before_seconds=5,
                tail_after_seconds=5,
                request_timeout_seconds=60,
            ),
        ),
    ),
)
