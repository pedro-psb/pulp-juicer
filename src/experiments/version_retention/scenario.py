"""Create candidate repositories and drive the SQL-seeded retention scenario."""

from pulp_juicer import PulpClient


def _repository(connection, run_id, retain_repo_versions):
    client = PulpClient(connection)
    name = f"retention-{run_id}"
    payload = {"name": name}
    if retain_repo_versions is not None:
        payload["retain_repo_versions"] = retain_repo_versions
    repository = client.wait_for_created_resource(
        client.post("/pulp/api/v3/repositories/file/file/", payload)
    )
    return {
        "repository": repository["pulp_href"],
        "repository_name": name,
        "retain_repo_versions": retain_repo_versions,
    }


def create_repository_retain_one(connection, fixtures, run_id):
    return _repository(connection, run_id, retain_repo_versions=1)


def create_repository_unlimited(connection, fixtures, run_id):
    return _repository(connection, run_id, retain_repo_versions=None)


def seed_sql(connection, fixtures, run_id, runtime):
    name = f"retention-{run_id}"
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
