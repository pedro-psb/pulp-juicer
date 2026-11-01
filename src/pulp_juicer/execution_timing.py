"""Extract non-overlapping trial phase durations from an execution log."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PHASES = (
    ("Preparation", "start", "pre_finished"),
    ("Locust startup", "pre_finished", "load_started"),
    ("Load test", "load_started", "load_stopped"),
    ("Locust shutdown", "load_stopped", "post_started"),
    ("Cleanup", "post_started", "finished"),
)

_ENTRY = re.compile(r"^(\S+) (?:INFO|WARNING|ERROR) (.*)$")
_START = re.compile(r"^Starting ([^/\s]+)/([^\s]+) (.+)$")
_FINISH = re.compile(r"^Finished ([^/\s]+)/([^\s]+) (.+?): ")
_EVENTS = {
    "Pre-test hooks finished": "pre_finished",
    "Load test started": "load_started",
    "Load test stopped": "load_stopped",
    "Post-test hooks started": "post_started",
}


@dataclass(frozen=True)
class TrialTiming:
    scenario: str
    run_id: str
    candidate: str
    durations_s: dict[str, float | None]
    total_s: float


def parse_execution_timings(path: Path) -> dict[tuple[str, str, str], TrialTiming]:
    """Return timings for completed trials; unavailable phases remain unknown."""
    if not path.is_file():
        return {}
    timings = {}
    current = None
    events = {}
    for line in path.read_text().splitlines():
        entry = _ENTRY.match(line)
        if entry is None:
            continue
        timestamp, message = entry.groups()
        start = _START.match(message)
        if start:
            scenario, run_id, candidate = start.groups()
            current = (scenario, candidate, run_id)
            events = {"start": datetime.fromisoformat(timestamp)}
            continue
        if current is None:
            continue
        if message in _EVENTS:
            events.setdefault(_EVENTS[message], datetime.fromisoformat(timestamp))
            continue
        finish = _FINISH.match(message)
        if finish is None:
            continue
        scenario, run_id, candidate = finish.groups()
        if current != (scenario, candidate, run_id):
            current = None
            continue
        events["finished"] = datetime.fromisoformat(timestamp)
        total_s = (events["finished"] - events["start"]).total_seconds()
        if total_s >= 0:
            durations = {}
            for phase, first, last in PHASES:
                duration = (
                    (events[last] - events[first]).total_seconds()
                    if first in events and last in events
                    else None
                )
                durations[phase] = (
                    duration if duration is not None and duration >= 0 else None
                )
            timings[current] = TrialTiming(
                scenario, run_id, candidate, durations, total_s
            )
        current = None
    return timings


_TIMING_SCHEMA = pa.schema(
    [
        pa.field("scenario", pa.string()),
        pa.field("candidate", pa.string()),
        pa.field("run_id", pa.string()),
        *(pa.field(phase, pa.float64()) for phase, _, _ in PHASES),
        pa.field("total_s", pa.float64()),
    ]
)


def normalize_execution_timings(execution_dir: Path) -> None:
    """Convert the diagnostic execution log to phase measurements once it changes."""
    source = execution_dir / "execution.log"
    if not source.is_file():
        return
    destination = execution_dir / "timings.parquet"
    if (
        destination.is_file()
        and destination.stat().st_mtime_ns >= source.stat().st_mtime_ns
    ):
        return
    rows = [
        {
            "scenario": timing.scenario,
            "candidate": timing.candidate,
            "run_id": timing.run_id,
            **{phase: timing.durations_s[phase] for phase, _, _ in PHASES},
            "total_s": timing.total_s,
        }
        for timing in parse_execution_timings(source).values()
    ]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_TIMING_SCHEMA),
        destination,
        compression="zstd",
    )


def read_execution_timings(
    execution_dir: Path,
) -> dict[tuple[str, str, str], TrialTiming]:
    """Read trial phase measurements from their normalized Parquet table."""
    path = execution_dir / "timings.parquet"
    if not path.is_file():
        return {}
    timings = {}
    for row in pq.read_table(path).to_pylist():
        timing = TrialTiming(
            row["scenario"],
            row["run_id"],
            row["candidate"],
            {phase: row[phase] for phase, _, _ in PHASES},
            row["total_s"],
        )
        timings[(timing.scenario, timing.candidate, timing.run_id)] = timing
    return timings
