"""Maven pull-through experiment definition."""

from pathlib import Path

from pulp_juicer import (
    AnalysisConfig,
    ExperimentConfig,
    LoadConfig,
    PluginSource,
    ScenarioConfig,
    ServiceFixtureConfig,
)

from . import candidates, scenario_input
from .service_maven_fixture import fixture


EXPERIMENT = ExperimentConfig(
    id="maven-pull-through",
    description="Compare Maven pull-through implementations under multiple loads.",
    fixture=ServiceFixtureConfig(
        compose_file=Path(__file__).resolve().parent / "service_maven_fixture/compose.yaml",
        service="maven_fixture",
        upstream_url="http://maven_fixture:8000/",
        generate=fixture.generate,
        options={"artifacts_per_run": 1000, "payload_bytes": 65536},
    ),
    candidates=(
        candidates.NormalCandidate(
            id=1,
            name="normal",
            baseline=True,
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
            driver="http_get",
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
            driver="http_get",
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
    ),
    analysis=AnalysisConfig(
        primary_metric="p95_response_time_ms",
        lower_is_better=True,
        minimum_practical_improvement_percent=10,
    ),
)
