#!/usr/bin/env python3
"""Check local sources before Compose starts an image build."""

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from packaging.specifiers import SpecifierSet
from packaging.version import Version


def is_git_url(source: str) -> bool:
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https", "ssh", "git"}:
        return bool(parsed.netloc and parsed.path)
    return source.startswith("git@") and ":" in source


def project(source: str, expected_name: str) -> dict | None:
    if is_git_url(source):
        return None
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"Source directory does not exist: {path}")
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise ValueError(f"Source is not a Git checkout: {path}")
    try:
        metadata = tomllib.loads((path / "pyproject.toml").read_text())["project"]
    except (FileNotFoundError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read project metadata in {path}: {exc}") from exc
    if metadata["name"] != expected_name:
        raise ValueError(f"Expected {expected_name} in {path}, got {metadata['name']}")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pulpcore", required=True)
    parser.add_argument("--maven", required=True)
    args = parser.parse_args()
    try:
        core = project(args.pulpcore, "pulpcore")
        maven = project(args.maven, "pulp-maven")
        if core is not None and maven is not None:
            requirements = [
                dependency for dependency in maven.get("dependencies", [])
                if dependency.lower().startswith("pulpcore")
            ]
            if len(requirements) != 1:
                raise ValueError("pulp-maven must declare one pulpcore dependency")
            specifier = SpecifierSet(requirements[0][len("pulpcore"):])
            if not specifier.contains(Version(core["version"]), prereleases=True):
                raise ValueError(
                    f"{args.maven} requires {requirements[0]}, but {args.pulpcore} "
                    f"is {core['version']}. Set PULP_MAVEN_REPO to a compatible source."
                )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    core_version = core["version"] if core is not None else "from Git URL"
    maven_version = maven["version"] if maven is not None else "from Git URL"
    print(f"Using pulpcore {core_version} and pulp-maven {maven_version}")
    if core is None or maven is None:
        print("Remote source compatibility will be checked during the image build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
