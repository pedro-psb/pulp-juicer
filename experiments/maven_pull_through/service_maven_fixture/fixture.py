"""Generate immutable Maven artifacts for the fixture service."""

import hashlib
import io
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def generate(root: Path, run_id: str, options: Mapping[str, Any]) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError("Run ID must contain only letters, numbers, underscores, or hyphens")
    count = int(options["artifacts_per_run"])
    payload_bytes = int(options["payload_bytes"])
    if count < 1 or payload_bytes < 1:
        raise ValueError("Fixture count and payload size must be positive")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for index in range(count):
        name = f"artifact-{index:06d}"
        path = f"org/example/loadtest/{run_id}/{name}/1.0/{name}-1.0.jar"
        payload = hashlib.shake_256(f"{run_id}:{index}".encode()).digest(payload_bytes)
        jar = io.BytesIO()
        with ZipFile(jar, "w", compression=ZIP_STORED) as archive:
            member = ZipInfo("fixture.bin", date_time=(2024, 1, 1, 0, 0, 0))
            archive.writestr(member, payload)
        data = jar.getvalue()
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(data).digest():
                raise ValueError(f"Fixture path already has different content: {destination}")
        else:
            destination.write_bytes(data)
        entries.append({"path": path, "sha256": hashlib.sha256(data).hexdigest()})
    return {"run_id": run_id, "artifacts": entries}

