#!/usr/bin/env python3
"""Create a deterministic Maven repository fixture."""

import argparse
import hashlib
import io
import json
import re
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-id", "--run-id", dest="fixture_id", required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--payload-bytes", type=int, default=65536)
    args = parser.parse_args()

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.fixture_id):
        parser.error("--fixture-id must contain only letters, numbers, underscores, or hyphens")
    if args.count < 1 or args.payload_bytes < 1:
        parser.error("--count and --payload-bytes must be positive")

    entries = {"staging": [], "normal": []}
    for candidate in entries:
        for number in range(args.count):
            artifact = f"artifact-{number:06d}"
            relative_path = (
                f"org/example/loadtest/{args.fixture_id}/{candidate}/{artifact}/1.0/"
                f"{artifact}-1.0.jar"
            )
            payload = hashlib.shake_256(
                f"{args.fixture_id}:{candidate}:{number}".encode()
            ).digest(args.payload_bytes)
            jar = io.BytesIO()
            with ZipFile(jar, "w", compression=ZIP_STORED) as archive:
                member = ZipInfo("fixture.bin", date_time=(2024, 1, 1, 0, 0, 0))
                archive.writestr(member, payload)
            data = jar.getvalue()
            destination = args.output / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            entries[candidate].append(
                {"path": relative_path, "sha256": hashlib.sha256(data).hexdigest()}
            )

    manifest = args.output / "manifest.json"
    manifest.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"Created {args.count} artifacts per candidate in {args.output}")
    print(f"Manifest: {manifest}")


if __name__ == "__main__":
    main()
