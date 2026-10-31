"""Configuration and extension points used by a pulp_juicer experiment."""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PulpConnection:
    base_url: str = "http://127.0.0.1:5001"
    username: str = "admin"
    password: str = "password"
    request_timeout_seconds: float = 30
    task_timeout_seconds: float = 120


@dataclass(frozen=True)
class ServiceFixtureMetadata:
    name: str
    compose_file: Path
    service_name: str
    output_dir: Path
    url: str
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VolumeFixtureMetadata:
    name: str
    output_dir: Path
    mount_path: str
    read_only: bool = True


FixtureMetadata = ServiceFixtureMetadata | VolumeFixtureMetadata


class Fixture(ABC):
    def __init__(self, name: str, output_dir: Path):
        self.name = name
        self.output_dir = Path(output_dir)

    @abstractmethod
    def build(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def generate_metadata(self) -> FixtureMetadata:
        raise NotImplementedError


class ServiceFixture(Fixture, ABC):
    @abstractmethod
    def generate_metadata(self) -> ServiceFixtureMetadata:
        raise NotImplementedError


class VolumeFixture(Fixture, ABC):
    @abstractmethod
    def generate_metadata(self) -> VolumeFixtureMetadata:
        raise NotImplementedError


@dataclass(frozen=True)
class CandidateConfig(ABC):
    id: int
    name: str
    sources: Mapping[str, str]

    @abstractmethod
    def setup(
        self,
        connection: PulpConnection,
        fixtures: Mapping[str, FixtureMetadata],
        run_id: str,
    ) -> dict:
        raise NotImplementedError


@dataclass(frozen=True)
class RequireFixture:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadConfig:
    users: int
    spawn_rate_per_second: float
    max_duration: str
    wait_seconds: float = 0
    idle_before_seconds: float = 5
    tail_after_seconds: float = 5
    request_timeout_seconds: float = 60
    request_count: int | None = None


@dataclass(frozen=True)
class ScenarioConfig:
    id: str
    description: str
    driver: str
    input: Callable[..., dict]
    run_order: tuple[int, ...]
    load: LoadConfig
    requires: tuple[RequireFixture, ...] = ()


@dataclass(frozen=True)
class ExperimentConfig:
    id: str
    short_description: str
    description: str
    required_sources: tuple[str, ...]
    fixtures: tuple[Fixture, ...]
    candidates: tuple[CandidateConfig, ...]
    scenarios: tuple[ScenarioConfig, ...]

    def __post_init__(self):
        if "pulpcore" not in self.required_sources:
            raise ValueError("Experiment must require pulpcore")
        if len(set(self.required_sources)) != len(self.required_sources):
            raise ValueError("Experiment has duplicate required source names")
        for name in self.required_sources:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError(f"Invalid source name: {name!r}")
        for candidate in self.candidates:
            self.validate_candidate_sources(candidate)

    def validate_candidate_sources(self, candidate: CandidateConfig):
        required = set(self.required_sources)
        supplied = set(candidate.sources)
        if required != supplied:
            missing = sorted(required - supplied)
            extra = sorted(supplied - required)
            raise ValueError(
                f"Candidate {candidate.name!r} sources differ from experiment requirements: "
                f"missing={missing}, extra={extra}"
            )
        for name, source in candidate.sources.items():
            if not source:
                raise ValueError(
                    f"Candidate {candidate.name!r} has no source for {name}"
                )
