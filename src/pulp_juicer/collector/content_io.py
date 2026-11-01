"""Per-worker process resource rates sampled about once per second.

Each row belongs to one Gunicorn content worker, identified by PID and process
start time. storage_*_bytes_per_s are changes in Linux process read_bytes and
write_bytes divided by the observed interval: bytes attributed to storage I/O,
which can differ from application writes due to caching and writeback.
read_syscalls_per_s/write_syscalls_per_s are read/write syscall rates; they are
not disk IOPS. These counters do not separate upstream traffic, responses to
Nginx, and temporary file access. The first sample of a new worker has null
rates because no previous counter value exists.

API-worker rows additionally report CPU percentage (100% is one full core),
CPU seconds spent during the interval, and proportional set size (PSS) in
bytes. PSS apportions shared pages across processes. These are process
measurements and do not include network bytes.
"""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents

SNAPSHOT_SCRIPT = Path(__file__).with_name("content_io_snapshot.py")
COUNTERS = ("read_bytes", "write_bytes", "read_count", "write_count")


class ContentIOSampler:
    """Turn per-worker Linux process counters into rates across samples."""

    def __init__(self, components: PulpComponents):
        self.components = components
        self.previous: dict[tuple[int, float], tuple[float, dict]] = {}
        self.worker_attribute = "content_workers"
        self.include_resources = False

    def _snapshot(self, pids: list[int]) -> list[dict]:
        result = subprocess.run(
            [
                "s6-setuidgid",
                "pulp",
                "/usr/bin/python3",
                str(SNAPSHOT_SCRIPT),
                *(("--resources",) if self.include_resources else ()),
                *(str(pid) for pid in pids),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def sample(self) -> list[dict]:
        """Return one rate row per live worker; omit workers that exited."""
        pids = [
            worker.pid for worker in getattr(self.components, self.worker_attribute)
        ]
        if not pids:
            self.previous.clear()
            return []

        snapshots = self._snapshot(pids)
        now = time.monotonic()
        current = {}
        rows = []
        for snapshot in snapshots:
            identity = (snapshot["pid"], snapshot["create_time"])
            prior = self.previous.get(identity)
            interval = now - prior[0] if prior is not None else None
            rates = (
                [
                    max(0, snapshot[name] - prior[1][name]) / interval
                    for name in (
                        *COUNTERS,
                        *(("cpu_seconds",) if self.include_resources else ()),
                    )
                ]
                if interval and interval > 0
                else [None] * (len(COUNTERS) + self.include_resources)
            )
            rows.append(
                {
                    "worker_pid": snapshot["pid"],
                    "worker_start_utc": datetime.fromtimestamp(
                        snapshot["create_time"], UTC
                    ),
                    "interval_s": interval,
                    "storage_read_bytes_per_s": rates[0],
                    "storage_write_bytes_per_s": rates[1],
                    "read_syscalls_per_s": rates[2],
                    "write_syscalls_per_s": rates[3],
                    **(
                        {
                            "cpu_pct": rates[4] * 100 if rates[4] is not None else None,
                            "cpu_seconds": rates[4] * interval if interval else None,
                            "pss_bytes": snapshot["pss_bytes"],
                        }
                        if self.include_resources
                        else {}
                    ),
                }
            )
            current[identity] = (now, snapshot)
        self.previous = current
        return rows


class ApiWorkerSampler(ContentIOSampler):
    """Sample API-worker CPU, proportional memory, and process I/O rates."""

    def __init__(self, components: PulpComponents):
        super().__init__(components)
        self.worker_attribute = "api_workers"
        self.include_resources = True
