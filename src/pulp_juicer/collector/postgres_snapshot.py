"""Read PostgreSQL CPU and PSS as the postgres user for the root collector."""

import json
import sys

import psutil


def snapshot(pid: int) -> list[dict]:
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []

    rows = []
    for process in processes:
        try:
            with process.oneshot():
                times = process.cpu_times()
                rows.append(
                    {
                        "pid": process.pid,
                        "create_time": process.create_time(),
                        "cpu_seconds": times.user + times.system,
                        "pss_bytes": process.memory_full_info().pss,
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return rows


if __name__ == "__main__":
    json.dump(snapshot(int(sys.argv[1])), sys.stdout)
