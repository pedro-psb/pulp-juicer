"""Manage the Pulp Compose services needed by planned runs."""

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO
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
        self.sources: dict[str, Path] = {}
        self.started = False

    def _command_args(self, files: tuple[Path, ...], *args: str) -> list[str]:
        command = [*self.plan.compose_command, "--project-name", self.project]
        for file in files:
            command.extend(("--file", str(file)))
        command.extend(args)
        return command

    def command(
        self,
        files: tuple[Path, ...],
        environment: Mapping[str, str],
        *args: str,
        check: bool = True,
        capture: bool = False,
        output: TextIO | None = None,
        timeout: float | None = None,
    ):
        command = self._command_args(files, *args)
        env = os.environ.copy()
        env.update(environment)
        return subprocess.run(
            command,
            cwd=self.plan.repository_root,
            env=env,
            check=check,
            text=True,
            timeout=timeout,
            **(
                {"stdout": output, "stderr": subprocess.STDOUT}
                if output is not None
                else {"capture_output": capture}
            ),
        )

    def destroy_before_start(self, *, reset: bool = False):
        """Remove any leftover project containers before preparing a run."""
        self.files = ()
        self.environment = {}
        self.sources = {}
        self.started = False
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

    def ensure(
        self,
        files: tuple[Path, ...],
        environment: dict[str, str],
        sources: Mapping[str, Path],
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
        elif self.sources != sources:
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
        self.sources = dict(sources)
        PulpClient(self.plan.pulp_connection).wait_ready(
            self.plan.ready_timeout_seconds
        )

    def close(self):
        if self.started:
            self.command(self.files, self.environment, "down", "--remove-orphans")
            self.started = False

    def dump_logs(self, path: Path) -> Path:
        """Save logs from every service before Compose removes the containers."""
        with path.open("w") as log:
            if not self.files:
                log.write("Compose services were not started for this run.\n")
                return path
            try:
                result = self.command(
                    self.files,
                    self.environment,
                    "logs",
                    "--no-color",
                    "--timestamps",
                    check=False,
                    output=log,
                )
            except OSError as exc:
                log.write(f"Could not collect Compose logs: {exc}\n")
            else:
                if result.returncode:
                    log.write(f"Compose logs exited with status {result.returncode}.\n")
        return path

    def copy_collector_outputs(self, destination: Path) -> Path:
        """Copy finalized collector output before removing the container."""
        destination.mkdir(parents=True, exist_ok=True)
        self.command(
            self.files,
            self.environment,
            "cp",
            "pulp:/var/lib/pgsql/juicer/.",
            str(destination),
        )
        return destination

    def trigger_collector(self, name: str, timeout_seconds: float = 15) -> str:
        """Ask the s6 collector to run a named job and await its reply."""
        return self._request_collector("collect", name, timeout_seconds)

    def wait_for_tasks(
        self,
        timeout_seconds: float = 600,
        on_progress: Callable[[dict], None] | None = None,
    ) -> str:
        """Wait for non-scheduled Pulp tasks to reach a terminal state."""
        return self._request_collector("wait-tasks", None, timeout_seconds, on_progress)

    def wait_for_postgres_idle(
        self,
        timeout_seconds: float = 120,
        on_progress: Callable[[dict], None] | None = None,
    ) -> str:
        """Wait for two consecutive PostgreSQL samples with zero CPU use."""
        return self._request_collector(
            "wait-postgres-idle", None, timeout_seconds, on_progress
        )

    def finalize_collectors(self, timeout_seconds: float = 15) -> str:
        """Stop periodic sampling and wait until Parquet footers are written."""
        return self._request_collector("finalize", None, timeout_seconds)

    def _request_collector(
        self,
        command: str,
        name: str | None,
        timeout_seconds: float,
        on_progress: Callable[[dict], None] | None = None,
    ) -> str:
        args = (
            "exec",
            "--no-TTY",
            "pulp",
            "/usr/bin/python3",
            "/usr/libexec/pulp-juicer/control.py",
            command,
            *((name,) if name else ()),
            "--timeout",
            str(timeout_seconds),
        )
        if command.startswith("wait-"):
            return self._stream_collector(args, timeout_seconds + 10, on_progress)
        result = self.command(
            self.files,
            self.environment,
            *args,
            capture=True,
            timeout=timeout_seconds + 5,
        )
        return result.stdout.strip()

    def _stream_collector(
        self,
        args: tuple[str, ...],
        timeout_seconds: float,
        on_progress: Callable[[dict], None] | None,
    ) -> str:
        command = self._command_args(self.files, *args)
        env = os.environ.copy()
        env.update(self.environment)
        output = []
        final = None
        expired = threading.Event()
        with subprocess.Popen(
            command,
            cwd=self.plan.repository_root,
            env=env,
            text=True,
            bufsize=1,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:

            def stop_on_timeout():
                if process.poll() is None:
                    expired.set()
                    process.kill()

            timer = threading.Timer(timeout_seconds, stop_on_timeout)
            timer.daemon = True
            timer.start()
            try:
                for line in process.stdout:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        output.append(line)
                        continue
                    if message.get("status") == "progress":
                        if on_progress:
                            on_progress(message["details"])
                    elif message.get("status") == "finished":
                        final = line.strip()
                    else:
                        output.append(line)
                returncode = process.wait()
            finally:
                timer.cancel()
        if expired.is_set():
            raise subprocess.TimeoutExpired(
                command, timeout_seconds, output="".join(output)
            )
        if returncode:
            raise subprocess.CalledProcessError(
                returncode, command, stderr="".join(output)
            )
        if final is None:
            raise RuntimeError("Collector did not return a final reply")
        return final
