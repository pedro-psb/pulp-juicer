"""Resolve local or Git plugin sources used by a test plan."""

import hashlib
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def plugin_environment_name(plugin: str) -> str:
    suffix = plugin.removeprefix("pulp_").replace("-", "_").upper()
    return f"PULP_{suffix}_REPO"


def source_details(source: str, path: Path) -> dict:
    """Describe the checkout actually used for a run."""
    parsed = urlsplit(source)
    if parsed.scheme in {"http", "https", "ssh", "git"}:
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        source = urlunsplit((parsed.scheme, host, parsed.path, "", parsed.fragment))
    revision = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "-C", str(path), "status", "--porcelain"], text=True
        ).strip()
    )
    return {"source": source, "revision": revision, "dirty": dirty}


def resolve_source(
    source: str, plugin: str, repository_root: Path, results_dir: Path
) -> Path:
    repository, separator, ref = source.partition("#")
    path = Path(repository).expanduser()
    if not repository.startswith(("https://", "http://", "ssh://", "git@", "file://")):
        if not path.is_absolute():
            path = repository_root / path
        if not path.is_dir():
            raise FileNotFoundError(f"Source directory does not exist: {path}")
        if not separator:
            return path.resolve()
        repository = str(path.resolve())

    key = hashlib.sha256(f"{repository}#{ref}".encode()).hexdigest()[:12]
    destination = results_dir / "_sources" / plugin / key
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        subprocess.run(
            ["git", "clone", "--no-checkout", repository, str(destination)], check=True
        )
    if separator:
        subprocess.run(
            ["git", "-C", str(destination), "fetch", "origin", ref], check=True
        )
        checkout = "FETCH_HEAD"
    else:
        subprocess.run(["git", "-C", str(destination), "fetch", "origin"], check=True)
        checkout = "origin/HEAD"
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--force", "--detach", checkout],
        check=True,
    )
    return destination.resolve()
