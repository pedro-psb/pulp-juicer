"""Small REST client for candidate setup functions."""

import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .types import PulpConnection


class PulpClient:
    def __init__(self, connection: PulpConnection):
        self.connection = connection
        self.base_url = connection.base_url.rstrip("/") + "/"
        token = f"{connection.username}:{connection.password}".encode()
        self.authorization = "Basic " + base64.b64encode(token).decode()

    def request(self, method: str, href: str, body: dict | None = None) -> dict:
        request = Request(
            urljoin(self.base_url, href if href.startswith("/") else f"/{href}"),
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": self.authorization,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlopen(
                request, timeout=self.connection.request_timeout_seconds
            ) as response:
                if method == "HEAD":
                    return dict(response.headers)
                content = response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"Pulp API returned HTTP {exc.code}: {detail}") from exc
        return json.loads(content) if content else {}

    def get(self, href: str) -> dict:
        return self.request("GET", href)

    def head(self, href: str) -> dict:
        """Return response headers for a HEAD request."""
        return self.request("HEAD", href)

    def post(self, href: str, body: dict | None = None) -> dict:
        return self.request("POST", href, body)

    def put(self, href: str, body: dict | None = None) -> dict:
        return self.request("PUT", href, body)

    def patch(self, href: str, body: dict | None = None) -> dict:
        return self.request("PATCH", href, body)

    def wait_ready(self, timeout_seconds: float = 300) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.get("/pulp/api/v3/status/")
                return
            except (OSError, RuntimeError, URLError) as exc:
                last_error = exc
                time.sleep(1)
        raise TimeoutError(
            f"Pulp did not become ready at {self.connection.base_url}: {last_error}"
        )

    def wait_for_created_resource(self, result: dict) -> dict:
        """Follow a Pulp task response and fetch its first created resource."""
        task_href = result.get("task")
        if not task_href:
            return result
        deadline = time.monotonic() + self.connection.task_timeout_seconds
        while True:
            task = self.get(task_href)
            if task.get("state") == "completed":
                resources = task.get("created_resources") or []
                if not resources:
                    raise RuntimeError(f"Pulp task created no resource: {task_href}")
                return self.get(resources[0])
            if task.get("state") in {"failed", "canceled"}:
                raise RuntimeError(f"Pulp task {task.get('state')}: {task}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Pulp task did not complete: {task_href}")
            time.sleep(min(1, remaining))
