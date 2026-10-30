"""Sample PostgreSQL process CPU from inside the Pulp container."""

import csv
import os
import select
import sys
import time
from datetime import UTC, datetime


def postgres_ticks():
    ticks = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"{entry.path}/comm") as stream:
                if stream.read().strip() not in {"postgres", "postmaster"}:
                    continue
            with open(f"{entry.path}/stat") as stream:
                fields = stream.read().rsplit(")", 1)[1].split()
            ticks[int(entry.name)] = int(fields[11]) + int(fields[12])
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return ticks


def main():
    print("READY", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip() != "start":
        return 2

    previous = postgres_ticks()
    if not previous:
        print("No PostgreSQL processes found", file=sys.stderr, flush=True)
        return 2

    clock_ticks = os.sysconf("SC_CLK_TCK")
    start = last_emit = time.monotonic()
    pending_cpu_seconds = 0.0
    writer = csv.writer(sys.stdout)
    writer.writerow(
        (
            "timestamp_utc",
            "elapsed_s",
            "interval_s",
            "postgres_cpu_pct",
            "postgres_cpu_seconds",
            "postgres_processes",
        )
    )
    sys.stdout.flush()
    print("STARTED", file=sys.stderr, flush=True)

    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
        now = time.monotonic()
        current = postgres_ticks()
        pending_cpu_seconds += (
            sum(max(0, ticks - previous.get(pid, 0)) for pid, ticks in current.items())
            / clock_ticks
        )
        previous = current

        elapsed = now - last_emit
        if elapsed >= 1 or ready:
            writer.writerow(
                (
                    datetime.now(UTC).isoformat(timespec="milliseconds"),
                    f"{now - start:.3f}",
                    f"{elapsed:.3f}",
                    f"{pending_cpu_seconds / elapsed * 100:.2f}" if elapsed else "0.00",
                    f"{pending_cpu_seconds:.4f}",
                    len(current),
                )
            )
            sys.stdout.flush()
            last_emit = now
            pending_cpu_seconds = 0.0

        if ready:
            if sys.stdin.readline().strip() != "stop":
                return 2
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
