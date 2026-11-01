"""Read worker I/O counters, and optional CPU and memory, as the pulp user.

Linux read_bytes/write_bytes count storage I/O attributed to a process. The
read_count/write_count values count read/write system calls, not disk IOPS or
network packets. The root collector invokes this helper as pulp because access
to another user's /proc/<pid>/io can be denied inside the container.
"""

import json
import sys

import psutil


def snapshot(pids: list[int], resources: bool = False) -> list[dict]:
    """Read each live worker's process counters while running as the pulp user."""
    rows = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                counters = process.io_counters()
                row = {
                    "pid": pid,
                    "create_time": process.create_time(),
                    "read_bytes": counters.read_bytes,
                    "write_bytes": counters.write_bytes,
                    "read_count": counters.read_count,
                    "write_count": counters.write_count,
                }
                if resources:
                    cpu = process.cpu_times()
                    row["cpu_seconds"] = cpu.user + cpu.system
                    row["pss_bytes"] = process.memory_full_info().pss
                rows.append(row)
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
    return rows


if __name__ == "__main__":
    resources = len(sys.argv) > 1 and sys.argv[1] == "--resources"
    json.dump(
        snapshot([int(arg) for arg in sys.argv[2 if resources else 1 :]], resources),
        sys.stdout,
    )
