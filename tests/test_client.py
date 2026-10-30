"""PulpClient HTTP methods and explicit Pulp task handling."""

import json
import unittest
from unittest.mock import patch

from pulp_juicer.client import PulpClient
from pulp_juicer.types import PulpConnection


class FakeResponse:
    def __init__(self, body=b"", headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class TestPulpClient(unittest.TestCase):
    def test_standard_http_methods(self):
        client = PulpClient(PulpConnection())
        responses = [
            FakeResponse(b'{"value": 1}'),
            FakeResponse(headers={"ETag": '"abc"'}),
            FakeResponse(b'{"task": "/tasks/1/"}'),
            FakeResponse(b'{"value": 2}'),
            FakeResponse(b'{"value": 3}'),
        ]
        with patch(
            "pulp_juicer.client.urlopen",
            side_effect=lambda *args, **kwargs: responses.pop(0),
        ) as urlopen:
            self.assertEqual(client.get("/items/1/"), {"value": 1})
            self.assertEqual(client.head("/items/1/"), {"ETag": '"abc"'})
            self.assertEqual(
                client.post("/items/", {"name": "a"}), {"task": "/tasks/1/"}
            )
            self.assertEqual(client.put("/items/1/", {"name": "b"}), {"value": 2})
            self.assertEqual(client.patch("/items/1/", {"name": "c"}), {"value": 3})

        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(
            [request.get_method() for request in requests],
            ["GET", "HEAD", "POST", "PUT", "PATCH"],
        )
        self.assertEqual(
            [json.loads(request.data) for request in requests[2:]],
            [
                {"name": "a"},
                {"name": "b"},
                {"name": "c"},
            ],
        )
        self.assertFalse(hasattr(client, "create"))

    def test_wait_for_created_resource_follows_pulp_task(self):
        client = PulpClient(PulpConnection())
        with (
            patch.object(
                client,
                "get",
                side_effect=[
                    {"state": "running"},
                    {"state": "completed", "created_resources": ["/items/1/"]},
                    {"pulp_href": "/items/1/"},
                ],
            ) as get,
            patch("pulp_juicer.client.time.sleep"),
        ):
            resource = client.wait_for_created_resource({"task": "/tasks/1/"})
        self.assertEqual(resource, {"pulp_href": "/items/1/"})
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "/tasks/1/",
                "/tasks/1/",
                "/items/1/",
            ],
        )
        self.assertEqual(
            client.wait_for_created_resource({"pulp_href": "/items/2/"}),
            {"pulp_href": "/items/2/"},
        )


if __name__ == "__main__":
    unittest.main()
