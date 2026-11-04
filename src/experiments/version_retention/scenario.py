"""Set up and drive the two retention scenarios."""

import time

from pulp_juicer import PulpClient


def _repository(connection, run_id):
    client = PulpClient(connection)
    name = f"retention-{run_id}"
    repository = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/repositories/file/file/",
            {
                "name": name,
                "retain_repo_versions": 1,
            },
        )
    )
    return client, name, repository["pulp_href"]


def _content_href(client):
    result = client.get(
        "/pulp/api/v3/content/file/files/?relative_path=seed%2F000000.bin"
    )
    if result["count"] != 1:
        raise RuntimeError(f"Expected one seed content unit, got {result['count']}")
    return result["results"][0]["pulp_href"]


def seed_file_sync(connection, fixtures, run_id, runtime):
    client, name, repository = _repository(connection, run_id)
    remote = client.wait_for_created_resource(
        client.post(
            "/pulp/api/v3/remotes/file/file/",
            {
                "name": name + "-remote",
                "url": fixtures["files"].url + "PULP_MANIFEST",
                "policy": "immediate",
            },
        )
    )["pulp_href"]
    result = client.post(repository + "sync/", {"remote": remote})
    deadline = time.monotonic() + 14400
    while True:
        task = client.get(result["task"])
        if task["state"] == "completed":
            break
        if task["state"] in {"failed", "canceled"}:
            raise RuntimeError(f"File sync {task['state']}: {task}")
        if time.monotonic() > deadline:
            raise TimeoutError("File sync exceeded four hours")
        time.sleep(5)
    latest = client.get(repository)["latest_version_href"]
    if (
        client.get(latest)["content_summary"]["present"]["file.file"]["count"]
        != 500_000
    ):
        raise RuntimeError("File sync did not create 500,000 members")
    return {
        "repository": repository,
        "content_href": _content_href(client),
        "api_base_url": connection.base_url,
    }


def seed_sql(connection, fixtures, run_id, runtime):
    _client, name, repository = _repository(connection, run_id)
    result = runtime.command(
        runtime.files,
        runtime.environment,
        "exec",
        "--no-TTY",
        "pulp",
        "/usr/bin/python3",
        "/opt/seed_models.py",
        name,
        capture=True,
        check=False,
        timeout=1800,
    )
    if result.returncode:
        raise RuntimeError(
            f"SQL seed failed with exit status {result.returncode}:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    content_id = result.stdout.strip().splitlines()[-1]
    return {
        "repository": repository,
        "content_href": f"/pulp/api/v3/content/file/files/{content_id}/",
        "api_base_url": connection.base_url,
    }


def change_one(fixtures, candidate, load):
    path = candidate["repository"].strip("/") + "/modify/"
    return {
        "base_url": candidate["api_base_url"].rstrip("/"),
        "requests": [
            {
                "path": path,
                "name": "retention-modify",
                "expected_status": 202,
                "json": {
                    "remove_content_units" if index % 2 == 0 else "add_content_units": [
                        candidate["content_href"]
                    ]
                },
            }
            for index in range(load.request_count)
        ],
    }
