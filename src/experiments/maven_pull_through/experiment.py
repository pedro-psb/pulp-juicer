"""Maven pull-through experiment definition."""

from pathlib import Path

from pulp_juicer import (
    ExperimentConfig,
    LoadConfig,
    PluginSource,
    RequireFixture,
    ScenarioConfig,
)

from . import candidates, scenario_input
from .service_maven_fixture.fixture import MavenServiceFixture

EXPERIMENT = ExperimentConfig(
    id="maven-pull-through",
    short_description="Compare Maven pull-through candidates under two load patterns.",
    description=(
        "Compare Maven pull-through implementations under burst and moderate request loads. "
        "Each candidate serves artifacts from the same generated upstream fixture, allowing "
        "response time and request completion to be compared across repeated runs."
    ),
    fixtures=(
        MavenServiceFixture(
            name="maven",
            output_dir=Path("fixture/maven"),
        ),
    ),
    candidates=(
        candidates.NormalCandidate(
            id=1,
            name="normal",
            source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
        ),
        candidates.StagingCandidate(
            id=2,
            name="staging",
            source=PluginSource(
                plugin="pulp_maven",
                git="https://github.com/your-fork/pulp_maven.git#staging-branch",
            ),
        ),
        candidates.StagingManualCandidate(
            id=3,
            name="staging-manual",
            source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
        ),
    ),
    scenarios=(
        ScenarioConfig(
            id="burst",
            description="Thirty concurrent users request 650 unique Maven artifacts.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            runs=(1, 2, 3, "r", 3, 2, 1, "r", 2, 1, 3),
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
            id="moderate",
            description="Ten concurrent users request 200 unique Maven artifacts.",
            driver="http_get",
            requires=(
                RequireFixture(name="maven", args={"count": 1000, "size": 65536}),
            ),
            input=scenario_input.build_pull_through,
            runs=(1, 2, 3, "r", 3, 2, 1),
            load=LoadConfig(
                users=10,
                request_count=200,
                spawn_rate_per_second=10,
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
            runs=(1,),
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
