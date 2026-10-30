"""Manage the Pulp Compose services needed by planned runs."""

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .client import PulpClient
from .plan import TestPlanMetadata


class ComposeRuntime:
    def __init__(self, plan: TestPlanMetadata):
        self.plan = plan
        self.project = re.sub(
            r"[^a-z0-9_-]+", "-", f"pulp-juicer-{plan.experiment.id}".lower()
        ).strip("-")[:63]
        self.files: tuple[Path, ...] = ()
        self.environment: dict[str, str] = {}
        self.source: tuple[str, Path] | None = None
        self.started = False

    def command(
        self,
        files: tuple[Path, ...],
        environment: Mapping[str, str],
        *args: str,
        check: bool = True,
        capture: bool = False,
    ):
        command = [*self.plan.compose_command, "--project-name", self.project]
        for file in files:
            command.extend(("--file", str(file)))
        command.extend(args)
        env = os.environ.copy()
        env.update(environment)
        return subprocess.run(
            command,
            cwd=self.plan.repository_root,
            env=env,
            check=check,
            text=True,
            capture_output=capture,
        )

    def destroy_before_start(self, *, reset: bool = False):
        """Remove any leftover project containers before Locust fires test_start."""
        environment = {
            "PULPCORE_REPO": str(self.plan.repository_root),
            "PULP_PORT": str(urlsplit(self.plan.pulp_connection.base_url).port or 5001),
            "PULP_PASSWORD": self.plan.pulp_connection.password,
        }
        args = (
            ("down", "--remove-orphans", "--volumes")
            if reset
            else ("down", "--remove-orphans")
        )
        self.command((self.plan.compose_file,), environment, *args)
        self.started = False

    def ensure(
        self,
        files: tuple[Path, ...],
        environment: dict[str, str],
        source: tuple[str, Path],
    ):
        service_env = {
            key: value
            for key, value in environment.items()
            if not key.endswith("_REPO") or key == "PULPCORE_REPO"
        }
        previous_service_env = {
            key: value
            for key, value in self.environment.items()
            if not key.endswith("_REPO") or key == "PULPCORE_REPO"
        }
        if self.started and (
            files != self.files or service_env != previous_service_env
        ):
            self.command(
                self.files, self.environment, "down", "--remove-orphans", check=False
            )
            self.started = False
        self.files = files
        self.environment = environment.copy()
        if not self.started:
            self.command(files, environment, "up", "--detach", "--build", "--wait")
            self.started = True
        elif self.source != source:
            self.command(
                files,
                environment,
                "up",
                "--detach",
                "--build",
                "--force-recreate",
                "--wait",
                "pulp",
            )
        self.source = source
        PulpClient(self.plan.pulp_connection).wait_ready(
            self.plan.ready_timeout_seconds
        )

    def close(self):
        if self.started:
            self.command(self.files, self.environment, "down", "--remove-orphans")
            self.started = False
