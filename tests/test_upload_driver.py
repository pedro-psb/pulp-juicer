"""Upload requests use multipart POST and keep Pulp credentials out of artifacts."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow.parquet as pq
import requests
from locust.clients import HttpSession

from pulp_juicer.locust_driver import run
from pulp_juicer.plan import RunMetadata
from pulp_juicer.types import UnitaryImpulseLoad


class TestUploadDriver(unittest.TestCase):
    def test_staging_upload_sends_repository_without_modify(self):
        sent = []

        def send(_session, method, url, **kwargs):
            sent.append((method, url, kwargs["data"]))
            response = requests.Response()
            response.status_code = 201
            response.url = url
            response._content = (
                b'{"pulp_href":"/pulp/api/v3/content/maven/artifact/456/"}'
            )
            return response

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact-1.0.jar"
            artifact.write_bytes(b"jar")
            metadata = RunMetadata(
                "smoke",
                "upload",
                SimpleNamespace(id=2, name="staging"),
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="5s"),
                "http_upload",
                lambda *args: {},
                root / "result",
            )
            with patch.object(HttpSession, "_send_request_safe_mode", send):
                result = run(
                    metadata,
                    lambda: {
                        "base_url": "http://example.test",
                        "auth": ("admin", "secret"),
                        "modify_repository": None,
                        "requests": [
                            {
                                "path": "pulp/api/v3/content/maven/artifact/upload/",
                                "relative_path": "org/example/artifact/1.0/artifact-1.0.jar",
                                "file_path": str(artifact),
                                "repository": "/pulp/api/v3/repositories/maven/maven/123/",
                                "expected_status": 201,
                            }
                        ],
                    },
                    lambda: None,
                )
            self.assertEqual(result.expected_requests, 1)
            self.assertEqual(result.completed_requests, 1)
            self.assertEqual(result.failures, 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "POST")
        self.assertEqual(
            sent[0][2]["repository"],
            "/pulp/api/v3/repositories/maven/maven/123/",
        )

    def test_upload_through_locust_without_network(self):
        prepared_requests = []

        def send(_session, method, url, **kwargs):
            prepared = requests.Request(
                method,
                url,
                data=kwargs.get("data"),
                files=kwargs.get("files"),
                json=kwargs.get("json"),
                auth=kwargs["auth"],
            ).prepare()
            prepared_requests.append(prepared)
            response = requests.Response()
            response.status_code = 202 if url.endswith("modify/") else 201
            response.request = prepared
            response.url = url
            response._content = (
                b"{}"
                if url.endswith("modify/")
                else json.dumps(
                    {
                        "pulp_href": "/pulp/api/v3/content/maven/artifact/"
                        + (
                            "456/"
                            if "artifact-1" in kwargs["data"]["relative_path"]
                            else "457/"
                        )
                    }
                ).encode()
            )
            return response

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact-1.0.jar"
            artifact.write_bytes(b"example-jar-payload")
            second_artifact = root / "artifact-2.0.jar"
            second_artifact.write_bytes(b"second-jar-payload")
            metadata = RunMetadata(
                "smoke",
                "upload",
                SimpleNamespace(id=1, name="normal"),
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=2, max_duration="5s"),
                "http_upload",
                lambda *args: {},
                root / "result",
            )
            with patch.object(HttpSession, "_send_request_safe_mode", send):
                result = run(
                    metadata,
                    lambda: {
                        "base_url": "http://example.test",
                        "auth": ("admin", "secret"),
                        "modify_repository": "/pulp/api/v3/repositories/maven/maven/123/",
                        "requests": [
                            {
                                "path": "pulp/api/v3/content/maven/artifact/upload/",
                                "name": "maven-upload",
                                "relative_path": "org/example/artifact/1.0/artifact-1.0.jar",
                                "file_path": str(artifact),
                                "expected_status": 201,
                            },
                            {
                                "path": "pulp/api/v3/content/maven/artifact/upload/",
                                "name": "maven-upload",
                                "relative_path": "org/example/artifact/2.0/artifact-2.0.jar",
                                "file_path": str(second_artifact),
                                "expected_status": 201,
                            },
                        ],
                    },
                    lambda: None,
                )
            self.assertEqual(result.completed_requests, 3)
            self.assertEqual(result.expected_requests, 3)
            self.assertEqual(result.failures, 0)
            rows = pq.read_table(metadata.result_dir / "data_load.parquet").to_pylist()
            self.assertEqual(
                [row["name"] for row in rows],
                ["maven-upload", "maven-upload", "maven-modify"],
            )
            self.assertNotIn("secret", str(rows))

        self.assertEqual(len(prepared_requests), 3)
        request = prepared_requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.url,
            "http://example.test/pulp/api/v3/content/maven/artifact/upload/",
        )
        self.assertTrue(
            request.headers["Content-Type"].startswith("multipart/form-data")
        )
        self.assertEqual(request.headers["Authorization"], "Basic YWRtaW46c2VjcmV0")
        self.assertIn(b"example-jar-payload", request.body)
        self.assertIn(b"org/example/artifact/1.0/artifact-1.0.jar", request.body)
        self.assertNotIn(b"/pulp/api/v3/repositories/maven/maven/123/", request.body)
        modify = prepared_requests[-1]
        self.assertEqual(
            modify.url,
            "http://example.test/pulp/api/v3/repositories/maven/maven/123/modify/",
        )
        self.assertEqual(
            json.loads(modify.body),
            {
                "add_content_units": [
                    "/pulp/api/v3/content/maven/artifact/456/",
                    "/pulp/api/v3/content/maven/artifact/457/",
                ]
            },
        )
