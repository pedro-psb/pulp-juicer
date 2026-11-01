"""Sample PostgreSQL CPU and PSS through the Pulp component registry."""

import json
import subprocess
import time
from pathlib import Path

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents

SNAPSHOT_SCRIPT = Path(__file__).with_name("postgres_snapshot.py")


def _unavailable() -> dict[str, int | float | None]:
    return {
        "interval_s": None,
        "cpu_pct": None,
        "cpu_seconds": None,
        "pss_bytes": None,
        "processes": 0,
    }


class PostgresSampler:
    def __init__(self, components: PulpComponents | None = None):
        self.components = components or PulpComponents()
        self.pid = None
        self.previous = None
        self.previous_at = None

    def _snapshot(self, pid: int) -> list[dict]:
        result = subprocess.run(
            [
                "s6-setuidgid",
                "postgres",
                "/usr/bin/python3",
                str(SNAPSHOT_SCRIPT),
                str(pid),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def sample(self) -> dict[str, int | float | None]:
        postgres = self.components.postgres
        pid = postgres.pid if postgres is not None else None
        if pid != self.pid:
            self.previous = None
            self.previous_at = None
            self.pid = pid
        if pid is None:
            return _unavailable()

        rows = self._snapshot(pid)
        if not rows:
            self.previous = None
            self.previous_at = None
            return _unavailable()

        current = {(row["pid"], row["create_time"]): row["cpu_seconds"] for row in rows}
        pss = sum(row["pss_bytes"] for row in rows)

        previous = self.previous
        previous_at = self.previous_at
        now = time.monotonic()
        self.previous = current
        self.previous_at = now
        cpu_seconds = (
            sum(
                max(0, seconds - previous.get(identity, 0))
                for identity, seconds in current.items()
            )
            if previous is not None
            else None
        )
        elapsed = now - previous_at if previous_at is not None else None
        return {
            "interval_s": elapsed,
            "cpu_pct": cpu_seconds / elapsed * 100 if elapsed else None,
            "cpu_seconds": cpu_seconds,
            "pss_bytes": pss,
            "processes": len(rows),
        }
