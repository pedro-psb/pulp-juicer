"""Live Pulp components discovered from s6 and Pulp's own settings."""

import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlunsplit

import psutil


@dataclass(frozen=True, slots=True)
class DatabaseConnection:
    name: str
    user: str
    host: str = ""
    port: str = ""
    password: str = field(default="", repr=False)

    @property
    def uri(self) -> str:
        """Build a libpq URI; an empty host keeps the default Unix socket."""
        authority = quote(self.user, safe="")
        if self.password:
            authority += ":" + quote(self.password, safe="")
        authority += "@"
        query = {}
        if self.host.startswith("/"):
            query["host"] = self.host
        else:
            authority += self.host
        if self.port:
            query["port"] = self.port
        return urlunsplit(
            (
                "postgresql",
                authority,
                "/" + quote(self.name, safe=""),
                urlencode(query),
                "",
            )
        )


@dataclass(frozen=True, slots=True)
class PulpComponent:
    kind: str
    service: str
    pid: int
    temp_dir: Path | None = None

    @property
    def process(self) -> psutil.Process:
        return psutil.Process(self.pid)

    def temp_files(self) -> tuple[Path, ...]:
        """Files currently inside this component's own temporary directory."""
        if self.temp_dir is None or not self.temp_dir.is_dir():
            return ()
        return tuple(
            sorted(path for path in self.temp_dir.rglob("*") if path.is_file())
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PostgresComponent(PulpComponent):
    connection: DatabaseConnection


class PulpComponents:
    """Resolve process IDs afresh so restarts and worker scaling are visible."""

    def __init__(
        self,
        service_root: Path = Path("/run/service"),
        *,
        working_directory: Path | None = None,
        worker_name_template: str | None = None,
        database: DatabaseConnection | None = None,
    ):
        self.service_root = service_root
        self._working_directory = working_directory
        self._worker_name_template = worker_name_template
        self._database = database

    def _settings(self):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pulpcore.app.settings")
        from django.conf import settings

        return settings

    @property
    def working_directory(self) -> Path:
        if self._working_directory is None:
            self._working_directory = Path(self._settings().WORKING_DIRECTORY)
        return self._working_directory

    @property
    def worker_name_template(self) -> str:
        if self._worker_name_template is None:
            self._worker_name_template = self._settings().WORKER_NAME_TEMPLATE
        return self._worker_name_template

    @property
    def database(self) -> DatabaseConnection:
        if self._database is None:
            config = self._settings().DATABASES["default"]
            self._database = DatabaseConnection(
                name=str(config["NAME"]),
                user=str(config["USER"]),
                host=str(config.get("HOST") or ""),
                port=str(config.get("PORT") or ""),
                password=str(config.get("PASSWORD") or ""),
            )
        return self._database

    def _pid(self, service: str) -> int | None:
        path = self.service_root / service
        if not path.exists():
            return None
        result = subprocess.run(
            ["s6-svstat", "-o", "pid", str(path)],
            capture_output=True,
            text=True,
            check=True,
        )
        pid = int(result.stdout.strip())
        return pid if pid > 0 else None

    def _gunicorn_workers(self, service: str, kind: str) -> tuple[PulpComponent, ...]:
        master_pid = self._pid(service)
        if master_pid is None:
            return ()
        try:
            children = psutil.Process(master_pid).children()
        except psutil.NoSuchProcess:
            return ()
        return tuple(
            PulpComponent(kind, service, child.pid)
            for child in sorted(children, key=lambda process: process.pid)
        )

    @property
    def api_workers(self) -> tuple[PulpComponent, ...]:
        return self._gunicorn_workers("pulpcore-api", "api-worker")

    @property
    def content_workers(self) -> tuple[PulpComponent, ...]:
        return self._gunicorn_workers("pulpcore-content", "content-worker")

    @property
    def task_workers(self) -> tuple[PulpComponent, ...]:
        workers = []
        for path in sorted(self.service_root.glob("pulpcore-worker@*")):
            pid = self._pid(path.name)
            if pid is None:
                continue
            name = self.worker_name_template.format(
                pid=pid, hostname=socket.gethostname(), fqdn=socket.getfqdn()
            )
            workers.append(
                PulpComponent(
                    "task-worker", path.name, pid, self.working_directory / name
                )
            )
        return tuple(workers)

    @property
    def postgres(self) -> PostgresComponent | None:
        pid = self._pid("postgresql")
        if pid is None:
            return None
        return PostgresComponent(
            "postgres", "postgresql", pid, connection=self.database
        )
