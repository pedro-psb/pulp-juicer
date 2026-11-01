"""Read content-worker I/O counters as the pulp user.

Linux read_bytes/write_bytes count storage I/O attributed to a process. The
read_count/write_count values count read/write system calls, not disk IOPS or
network packets. The root collector invokes this helper as pulp because access
to another user's /proc/<pid>/io can be denied inside the container.
"""

import json
import sys

import psutil


def snapshot(pids: list[int]) -> list[dict]:
    """Read each live worker's I/O counters while running as the pulp user."""
    rows = []
    for pid in pids:
        try:
            process = psutil.Process(pid)
            with process.oneshot():
                counters = process.io_counters()
                rows.append(
                    {
                        "pid": pid,
                        "create_time": process.create_time(),
                        "read_bytes": counters.read_bytes,
                        "write_bytes": counters.write_bytes,
                        "read_count": counters.read_count,
                        "write_count": counters.write_count,
                    }
                )
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
    return rows


if __name__ == "__main__":
    json.dump(snapshot([int(arg) for arg in sys.argv[1:]]), sys.stdout)
