"""Configuration and extension points used by a pulp_juicer experiment."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PluginSource:
    plugin: str
    git: str


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
    source: PluginSource

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
    runs: tuple[int | str, ...]
    load: LoadConfig
    requires: tuple[RequireFixture, ...] = ()


@dataclass(frozen=True)
class ExperimentConfig:
    id: str
    short_description: str
    description: str
    fixtures: tuple[Fixture, ...]
    candidates: tuple[CandidateConfig, ...]
    scenarios: tuple[ScenarioConfig, ...]
