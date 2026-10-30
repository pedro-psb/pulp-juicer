"""Record setup and load settings alongside each candidate run."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def source_details(source):
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https", "ssh", "git"}:
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return {"source": urlunsplit((parsed.scheme, host, parsed.path, "", parsed.fragment)), "revision": parsed.fragment or "remote ref", "dirty": None}
    if source.startswith("git@"):
        return {"source": source, "revision": source.split("#", 1)[1] if "#" in source else "remote ref", "dirty": None}
    path = Path(source).expanduser().resolve()
    revision = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", str(path), "status", "--porcelain"], text=True).strip())
    return {"source": str(path), "revision": revision, "dirty": dirty}


def setup(args):
    manifest_path = args.manifest
    manifest = json.loads(manifest_path.read_text())
    counts = {candidate: len(artifacts) for candidate, artifacts in manifest.items()}
    write_json(args.run_dir / "setup_metadata.json", {
        "fixture": {
            "counts": counts,
            "requested_count": args.count,
            "requested_fixture_id": args.fixture_id,
            "requested_payload_bytes": args.payload_bytes,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "sources": {
            "pulpcore": source_details(args.pulpcore),
            "pulp-maven": source_details(args.maven),
        },
        "project": args.project,
        "upstream_url": args.upstream_url,
    })


def run(args):
    setup_dir = args.setup_dir
    setup_metadata = json.loads((setup_dir / "setup_metadata.json").read_text())
    setup_config = json.loads((setup_dir / "setup.json").read_text())
    manifest = json.loads(args.manifest.read_text())
    if args.candidate not in setup_config or args.candidate not in manifest:
        raise ValueError(f"Candidate {args.candidate!r} is missing from setup or fixture")
    actual_hash = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    if actual_hash != setup_metadata["fixture"]["manifest_sha256"]:
        raise ValueError("Fixture changed after setup; run make up with a new RUN_ID")

    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.manifest, output / "manifest.json")
    shutil.copyfile(setup_dir / "setup.json", output / "setup.json")
    write_json(output / "run_metadata.json", {
        "run_id": setup_dir.name,
        "candidate": args.candidate,
        "fixture": {
            "count": len(manifest[args.candidate]),
            **{key: value for key, value in setup_metadata["fixture"].items() if key != "counts"},
        },
        "load": {
            "users": args.users,
            "spawn_rate": args.spawn_rate,
            "duration": args.duration,
            "wait_seconds": args.wait_seconds,
            "idle_seconds": args.idle_seconds,
            "tail_seconds": args.tail_seconds,
        },
        "sources": setup_metadata["sources"],
        "project": setup_metadata["project"],
        "upstream_url": setup_metadata["upstream_url"],
        "setup": setup_config[args.candidate],
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--run-dir", type=Path, required=True)
    setup_parser.add_argument("--manifest", type=Path, required=True)
    setup_parser.add_argument("--pulpcore", required=True)
    setup_parser.add_argument("--maven", required=True)
    setup_parser.add_argument("--count", type=int, required=True)
    setup_parser.add_argument("--fixture-id", required=True)
    setup_parser.add_argument("--payload-bytes", type=int, required=True)
    setup_parser.add_argument("--project", required=True)
    setup_parser.add_argument("--upstream-url", required=True)

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--setup-dir", type=Path, required=True)
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--output-dir", type=Path, required=True)
    run_parser.add_argument("--candidate", required=True)
    run_parser.add_argument("--users", type=int, required=True)
    run_parser.add_argument("--spawn-rate", type=float, required=True)
    run_parser.add_argument("--duration", required=True)
    run_parser.add_argument("--wait-seconds", type=float, required=True)
    run_parser.add_argument("--idle-seconds", type=float, required=True)
    run_parser.add_argument("--tail-seconds", type=float, required=True)
    args = parser.parse_args()
    if args.command == "setup":
        setup(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
