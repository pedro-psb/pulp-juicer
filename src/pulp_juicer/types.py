"""Configuration and extension points used by a pulp_juicer experiment."""

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
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
class PreTestFunction:
    description: str
    setup_fn: Callable[
        [PulpConnection, Mapping[str, FixtureMetadata], str], Mapping[str, Any] | None
    ]


@dataclass(frozen=True)
class CandidateConfig:
    id: int
    name: str
    description: str
    sources: Mapping[str, str]
    pre_test_functions: list[PreTestFunction]
    pulp_settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        for name in self.pulp_settings:
            if (
                not isinstance(name, str)
                or not name.isidentifier()
                or not name.isupper()
            ):
                raise ValueError(f"Invalid candidate Pulp setting name: {name!r}")


@dataclass(frozen=True)
class RequireFixture:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnitaryImpulseLoad:
    request_count: int
    max_duration: str = "5m"
    wait_seconds: float = 0
    request_timeout_seconds: float = 60

    def __post_init__(self):
        if self.request_count < 1:
            raise ValueError("Impulse request count must be positive")

    @property
    def users(self) -> int:
        return self.request_count

    @property
    def spawn_rate_per_second(self) -> float:
        return float(self.request_count)


@dataclass(frozen=True)
class LoadStep:
    at_seconds: float
    users: int


@dataclass(frozen=True)
class StepFunctionLoad:
    steps: tuple[LoadStep, ...]
    max_duration: str = "5m"
    wait_seconds: float = 0
    request_timeout_seconds: float = 60
    request_count: int | None = None

    def __post_init__(self):
        if not self.steps or self.steps[0].at_seconds != 0:
            raise ValueError("Step function must start at 0 seconds")
        if any(step.users < 0 for step in self.steps):
            raise ValueError("Step user counts must be nonnegative")
        if any(
            later.at_seconds <= earlier.at_seconds
            for earlier, later in zip(self.steps, self.steps[1:])
        ):
            raise ValueError("Step times must increase")

    @property
    def users(self) -> int:
        return max(step.users for step in self.steps)

    @staticmethod
    def spawn_rate(from_users: int, to_users: int) -> float:
        return float(max(1, abs(to_users - from_users)))


LoadProfile = UnitaryImpulseLoad | StepFunctionLoad


@dataclass(frozen=True)
class ScenarioConfig:
    id: str
    description: str
    driver: str
    input: Callable[..., dict]
    run_order: tuple[int, ...]
    load: LoadProfile
    requires: tuple[RequireFixture, ...] = ()
    draft: bool = False
    selected_stats: Sequence[str] = ()
    setup_fn: Callable[..., Mapping[str, Any] | None] | None = None

    def __post_init__(self):
        if (
            not isinstance(self.run_order, tuple)
            or not self.run_order
            or any(type(candidate) is not int for candidate in self.run_order)
        ):
            raise ValueError(
                "Scenario run_order must be a nonempty tuple of candidate IDs"
            )
        if not isinstance(self.selected_stats, (tuple, list)) or any(
            not isinstance(key, str) or not key for key in self.selected_stats
        ):
            raise ValueError("Scenario selected_stats must be a list of variable keys")
        if len(set(self.selected_stats)) != len(self.selected_stats):
            raise ValueError("Scenario selected_stats must not contain duplicates")
        object.__setattr__(self, "selected_stats", tuple(self.selected_stats))


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
