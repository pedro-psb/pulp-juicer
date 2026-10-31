"""Assign numbered IDs to squeeze executions."""

import fcntl
import os
import re
from pathlib import Path

_NUMBERED_EXECUTION = re.compile(r"^.+__(\d{3,})$")


def _existing_ids(workdir: Path) -> set[str]:
    ids = {
        path.name.removeprefix("plan-").removesuffix(".json")
        for path in workdir.glob("plan-*.json")
    }
    results_dir = workdir / "results"
    if results_dir.is_dir():
        ids.update(path.name for path in results_dir.iterdir() if path.is_dir())
    return {
        execution_id
        for execution_id in ids
        if _NUMBERED_EXECUTION.fullmatch(execution_id)
    }


def next_execution_id(workdir: Path, scope: str) -> str:
    """Reserve the next workspace-wide number, including after results are cleaned."""
    workdir.mkdir(parents=True, exist_ok=True)
    with (workdir / ".execution-sequence").open("a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0)
            saved = stream.read().strip()
            if saved and not saved.isdecimal():
                raise ValueError("Invalid execution sequence")
            existing = max(
                (
                    int(match.group(1))
                    for item in _existing_ids(workdir)
                    if (match := _NUMBERED_EXECUTION.fullmatch(item))
                ),
                default=0,
            )
            number = max(int(saved or 0), existing) + 1
            stream.seek(0)
            stream.truncate()
            stream.write(f"{number}\n")
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
    return f"{scope}__{number:03d}"


def latest_execution_id(workdir: Path, scope: str) -> str | None:
    """Find the latest saved or reserved execution for a selected scope."""
    prefix = f"{scope}__"
    matches = [
        item
        for item in _existing_ids(workdir)
        if item.startswith(prefix) and item[len(prefix) :].isdigit()
    ]
    return max(matches, key=lambda item: int(item[len(prefix) :]), default=None)
