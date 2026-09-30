"""Run with: python -m unittest discover -s tools/runpod_tester -p test_server.py.

Only the loopback HTTP server is real. Every Runpod network request is mocked;
these tests never submit jobs, spend GPU credit, or use real API credentials.
"""

import base64
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request


MODULE_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("runpod_tester_under_test", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)
ROOT = MODULE_PATH.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Response(io.BytesIO):
    status = 200


def upstream_response(document):
    return Response(json.dumps(document).encode("utf-8"))


class EndpointTests(unittest.TestCase):
    def test_endpoint_ids_and_official_urls_normalize(self):
        for value in (
            "endpoint_12-AB", " endpoint_12-AB ",
            "https://api.runpod.ai/v2/endpoint_12-AB",
            "https://api.runpod.ai:443/v2/endpoint_12-AB/",
            "https://api.runpod.ai/v2/endpoint_12-AB/run",
            "https://api.runpod.ai/v2/endpoint_12-AB/runsync/",
            "https://api.runpod.ai/v2/endpoint_12-AB/health",
        ):
            with self.subTest(value=value):
                self.assertEqual(server.normalize_endpoint(value), "endpoint_12-AB")

    def test_arbitrary_hosts_paths_and_url_credentials_are_rejected(self):
        secret = "private-token-should-not-appear"
        for value in (
            "", None, 123, [], "a" * 129, "one/two", "one?token=" + secret,
            "http://api.runpod.ai/v2/endpoint", "https://evil.example/v2/endpoint",
            "https://api.runpod.ai.evil.example/v2/endpoint",
            "https://api.runpod.ai:444/v2/endpoint",
            f"https://user:{secret}@api.runpod.ai/v2/endpoint",
            f"https://api.runpod.ai/v2/endpoint?api_key={secret}",
            f"https://api.runpod.ai/v2/endpoint#{secret}",
            "https://api.runpod.ai/v2/endpoint/status/job",
            "https://api.runpod.ai/v2/endpoint/../other",
            "https://api.runpod.ai/v2/endpoint%2Fother",
            "https://api.runpod.ai/v2/endpoint%0d%0aAuthorization:bad",
            "https://a-pod.proxy.runpod.net/",
        ):
            with self.subTest(value=value):
                with self.assertRaises(server.TesterError) as raised:
                    server.normalize_endpoint(value)
                self.assertNotIn(secret, str(raised.exception))

    def test_job_id_is_a_single_bounded_path_component(self):
        valid = "test-01234567-89ab-cdef-u1"
        self.assertEqual(server.validate_job_id(valid), valid)
        for value in (None, [], 42, "", "a" * 201, "../job", "a/b", "a%2fb",
                      "a?x=y", "a#fragment", "a b", "a\r\nAuthorization:x"):
            with self.subTest(value=value), self.assertRaises(server.TesterError):
                server.validate_job_id(value)


class ConfigTests(unittest.TestCase):
    def test_config_matches_real_worker_manifest_and_hides_credentials(self):
        manifest = json.loads((ROOT / "workflows/manifest.json").read_text())
        config = server.get_config("endpoint", api_key_configured=True)
        self.assertEqual(config["endpoint_id"], "endpoint")
        self.assertIs(config["api_key_configured"], True)
        self.assertNotIn("api_key", config)
        self.assertEqual(len(config["modes"]), len(manifest["modes"]))
        self.assertEqual({mode["id"] for mode in config["modes"]},
                         set(manifest["modes"]))
        self.assertEqual(config["max_run_bytes"], 9_000_000)
        for mode in config["modes"]:
            with self.subTest(mode=mode["id"]):
                self.assertLessEqual(mode["parameters"]["rules"]["seed"]["maximum"],
                                     2**53 - 1)
                self.assertEqual(mode["parameters"]["defaults"]["num_frames"], 9)
                self.assertEqual(mode["output_scale"],
                                 manifest["modes"][mode["id"]].get("output_scale", 1))

    def test_ui_defaults_pass_actual_adapter_validation_without_media_downloads(self):
        from ltx_worker.adapter import prepare_input

        for mode in server.get_config()["modes"]:
            with self.subTest(mode=mode["id"]):
                params = dict(mode["parameters"]["defaults"])
                if "tracks_json" in params:
                    params["tracks_json"] = json.dumps([
                        [{"x": 1, "y": 1} for _ in range(params["num_frames"])]
                    ])
                payload = {
                    "mode": mode["id"], "parameters": params,
                    "media": {item["role"]: {"base64": "not-decoded-in-this-test"}
                              for item in mode["media"] if item["required"]},
                }
                # prepare_input validates bindings/settings but does not ingest media.
                spec, actual, media, graph = prepare_input(payload)
                self.assertEqual(actual["num_frames"], 9)
                self.assertTrue(graph)
                self.assertEqual(set(media),
                                 {role for role, item in spec["media"].items()
                                  if item.get("required", True)})

    def test_cq_runtime_has_an_isolated_safe_first_preset(self):
        config = server.get_config("cq-endpoint", runtime="cq-v2")
        self.assertEqual(config["runtime"], "cq-v2")
        self.assertFalse(config["validation_preset_available"])
        self.assertEqual([mode["id"] for mode in config["modes"]],
                         ["video_enhance_cq_v2"])
        mode = config["modes"][0]
        self.assertFalse(mode["prompt_required"])
        self.assertEqual(mode["parameters"]["defaults"]["fps"], 30)
        self.assertEqual(mode["parameters"]["defaults"]["num_frames"], 33)
        self.assertEqual(mode["transport"], "named")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.key = "test-secret-must-stay-private"
        self.client = server.RunpodClient(self.key)
        self.client.opener = Mock()
        self.client.opener.open.return_value = upstream_response({"status": "OK"})

    def test_fixed_origin_methods_and_request_envelopes(self):
        for action, method, suffix in (
            ("health", "GET", "/health"), ("run", "POST", "/run"),
            ("status", "GET", "/status/job-123"),
            ("cancel", "POST", "/cancel/job-123"),
        ):
            with self.subTest(action=action):
                self.client.opener.reset_mock()
                self.client.opener.open.return_value = upstream_response({"status": "OK"})
                supplied = {"mode": "text_to_video", "prompt": "Small smoke test"}
                result = self.client.request(
                    action, "endpoint", job_id="job-123", job_input=supplied
                )
                self.assertEqual(result, {"status": "OK"})
                self.client.opener.open.assert_called_once()
                request = self.client.opener.open.call_args.args[0]
                self.assertEqual(request.full_url,
                                 "https://api.runpod.ai/v2/endpoint" + suffix)
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
                self.assertEqual(self.client.opener.open.call_args.kwargs["timeout"], 40)
                if action == "run":
                    self.assertEqual(json.loads(request.data), {"input": supplied})
                else:
                    self.assertIsNone(request.data)

    def test_request_key_overrides_configured_key(self):
        self.client.request("health", "endpoint", api_key="request-secret")
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer request-secret")

    def test_cq_request_uses_named_input_and_long_job_policy(self):
        client = server.RunpodClient(self.key, runtime="cq-v2")
        client.opener = Mock()
        client.opener.open.return_value = upstream_response({"id": "cq-job"})
        supplied = {
            "mode": "video_enhance_cq_v2",
            "media": {"video": {"url": "https://bucket.example/source.mp4"}},
        }
        self.assertEqual(client.request("run", "endpoint", job_input=supplied),
                         {"id": "cq-job"})
        envelope = json.loads(client.opener.open.call_args.args[0].data)
        self.assertEqual(envelope["input"], supplied)
        self.assertEqual(envelope["policy"], {
            "executionTimeout": 3_600_000, "ttl": 7_200_000,
        })

    def test_proxy_environment_is_not_used(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://untrusted.invalid:9999"}), \
                patch.object(server.urllib.request, "build_opener") as build:
            server.RunpodClient(self.key)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], server.NoRedirect)

    def test_redirect_rejected_before_forwarding_auth(self):
        request = urllib.request.Request(
            "https://api.runpod.ai/v2/endpoint/run", data=b"{}", method="POST",
            headers={"Authorization": "Bearer " + self.key},
        )
        with self.assertRaises(server.TesterError) as raised:
            server.NoRedirect().redirect_request(
                request, None, 307, "redirect", {}, "https://evil.example/" + self.key
            )
        self.assertTrue(raised.exception.submission_uncertain)
        self.assertNotIn(self.key, str(raised.exception))

    def test_validation_errors_never_contact_runpod(self):
        cases = [
            ("unknown", "endpoint", {}),
            ("health", "https://evil.example/v2/endpoint", {}),
            ("status", "endpoint", {"job_id": "../job"}),
            ("cancel", "endpoint", {}),
            ("run", "endpoint", {"job_input": None}),
            ("run", "endpoint", {"job_input": {}}),
            ("run", "endpoint", {"job_input": {"mode": "unavailable"}}),
            ("run", "endpoint", {"job_input": {"mode": "image_to_video", "workflow": {}}}),
            ("run", "endpoint", {"job_input": {"mode": []}}),
            ("run", "endpoint", {"job_input": {"mode": {"invalid": True}}}),
            ("run", "endpoint", {"job_input": {"mode": "text_to_video", "seed": float("nan")}}),
        ]
        for action, endpoint, kwargs in cases:
            with self.subTest(action=action, kwargs=kwargs):
                with self.assertRaises(server.TesterError):
                    self.client.request(action, endpoint, **kwargs)
        self.client.opener.open.assert_not_called()

    def test_missing_or_header_unsafe_api_key_is_rejected(self):
        for key in ("", "has space", "line\r\ninjection", "x" * 513, "non-ascii-é", 42, []):
            with self.subTest(key=key):
                client = server.RunpodClient()
                client.opener = Mock()
                with self.assertRaises(server.TesterError) as raised:
                    client.request("health", "endpoint", api_key=key)
                self.assertEqual(raised.exception.status,
                                 401 if isinstance(key, str) else 400)
                client.opener.open.assert_not_called()

    def test_encoded_run_size_is_enforced_before_network(self):
        payload = {"mode": "text_to_video", "prompt": "é" * 100}
        with patch.object(server, "MAX_RUN_BYTES", 200):
            with self.assertRaises(server.TesterError) as raised:
                self.client.request("run", "endpoint", job_input=payload)
        self.assertEqual(raised.exception.status, 413)
        self.client.opener.open.assert_not_called()

    def test_auth_errors_do_not_return_or_log_upstream_secrets(self):
        for code in (401, 403):
            with self.subTest(code=code):
                self.client.opener.reset_mock()
                self.client.opener.open.side_effect = urllib.error.HTTPError(
                    "https://api.runpod.ai/v2/endpoint/run?token=" + self.key,
                    code, self.key, {}, io.BytesIO(self.key.encode()),
                )
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                    with self.assertRaises(server.TesterError) as raised:
                        self.client.request("run", "endpoint", job_input={"mode": "text_to_video"})
                self.assertEqual(raised.exception.status, code)
                self.assertFalse(raised.exception.submission_uncertain)
                self.assertNotIn(self.key, str(raised.exception) + captured.getvalue())
                self.client.opener.open.assert_called_once()

    def test_ambiguous_post_failure_is_not_retried(self):
        for error in (TimeoutError(self.key), socket.timeout(self.key),
                      urllib.error.URLError(self.key),
                      urllib.error.HTTPError("https://api.runpod.ai", 503,
                                             self.key, {}, io.BytesIO(b"unavailable"))):
            with self.subTest(error=type(error).__name__):
                self.client.opener.reset_mock()
                self.client.opener.open.side_effect = error
                with self.assertRaises(server.TesterError) as raised:
                    self.client.request("run", "endpoint", job_input={"mode": "text_to_video"})
                self.assertTrue(raised.exception.submission_uncertain)
                self.assertNotIn(self.key, str(raised.exception))
                self.client.opener.open.assert_called_once()

    def test_get_timeout_does_not_claim_a_submission(self):
        self.client.opener.open.side_effect = TimeoutError(self.key)
        with self.assertRaises(server.TesterError) as raised:
            self.client.request("status", "endpoint", job_id="job")
        self.assertEqual(raised.exception.status, 504)
        self.assertFalse(raised.exception.submission_uncertain)

    def test_unreadable_or_oversized_submission_response_is_uncertain(self):
        for raw in (b"not-json", b"[]", b'{"bad":NaN}'):
            with self.subTest(raw=raw):
                self.client.opener.reset_mock()
                self.client.opener.open.return_value = Response(raw)
                with self.assertRaises(server.TesterError) as raised:
                    self.client.request("run", "endpoint", job_input={"mode": "text_to_video"})
                self.assertTrue(raised.exception.submission_uncertain)
                self.client.opener.open.assert_called_once()
        with patch.object(server, "MAX_RESPONSE_BYTES", 16):
            self.client.opener.open.return_value = Response(b"x" * 17)
            with self.assertRaises(server.TesterError) as raised:
                self.client.request("run", "endpoint", job_input={"mode": "text_to_video"})
            self.assertTrue(raised.exception.submission_uncertain)

    def test_nested_upstream_errors_redact_key_without_altering_status(self):
        self.client.opener.open.return_value = upstream_response({
            "status": "FAILED", "error": "Invalid token " + self.key,
            "output": {"details": ["Bearer " + self.key, {"message": self.key}],
                       "upstream-" + self.key: "redact dictionary keys too"},
        })
        result = self.client.request("status", "endpoint", job_id="job")
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn(self.key, json.dumps(result))
        self.assertIn("[redacted]", json.dumps(result))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server(port=0, endpoint_id="configured-endpoint",
                                       api_key="configured-secret")
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.httpd.endpoint_id = "configured-endpoint"
        self.httpd.client = server.RunpodClient("configured-secret")
        self.httpd.client.opener = Mock()
        self.httpd.client.opener.open.return_value = upstream_response({"status": "OK"})

    def request(self, path, body=None, method=None, headers=None):
        actual_headers = {} if headers is None else dict(headers)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            actual_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request(method or ("POST" if body is not None else "GET"), path,
                         body=body, headers=actual_headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_server_binds_loopback_and_config_exposes_only_key_boolean(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")
        status, headers, raw = self.request("/api/config")
        self.assertEqual(status, 200)
        config = json.loads(raw)
        self.assertTrue(config["api_key_configured"])
        self.assertNotIn(b"configured-secret", raw)
        self.assertNotIn("api_key", config)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.httpd.client.opener.open.assert_not_called()

    def test_sample_image_is_real_png(self):
        status, _, raw = self.request("/api/sample-image")
        self.assertEqual(status, 200)
        image = base64.b64decode(json.loads(raw)["base64"], validate=True)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.httpd.client.opener.open.assert_not_called()

    def test_all_proxy_routes_map_to_fixed_upstream_urls(self):
        for action, suffix in (("health", "/health"), ("run", "/run"),
                               ("status", "/status/job"), ("cancel", "/cancel/job")):
            with self.subTest(action=action):
                self.httpd.client.opener.reset_mock()
                self.httpd.client.opener.open.return_value = upstream_response({"id": "job"})
                body = {"input": {"mode": "text_to_video"}, "job_id": "job"}
                status, _, raw = self.request("/api/" + action, body)
                self.assertEqual(status, 200, raw)
                self.assertEqual(json.loads(raw), {"id": "job"})
                request = self.httpd.client.opener.open.call_args.args[0]
                self.assertEqual(request.full_url,
                                 "https://api.runpod.ai/v2/configured-endpoint" + suffix)

    def test_host_origin_and_fetch_site_guards(self):
        for headers in (
            {"Host": "evil.example"}, {"Host": "127.0.0.1:1"},
            {"Origin": "https://evil.example"},
            {"Origin": "http://localhost:" + str(self.port)},
            {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
        ):
            with self.subTest(headers=headers):
                status, _, _ = self.request("/api/health", {}, headers=headers)
                self.assertEqual(status, 403)
        self.httpd.client.opener.open.assert_not_called()
        status, _, _ = self.request("/api/health", {}, headers={
            "Origin": "http://127.0.0.1:" + str(self.port),
            "Sec-Fetch-Site": "same-origin",
        })
        self.assertEqual(status, 200)

    def test_duplicate_host_and_content_length_are_rejected(self):
        # Local transport on some Windows hosts merges duplicate Host headers.
        # Exercise the parser/guard directly so that normalization cannot mask it.
        host = "127.0.0.1:" + str(self.port)
        handler = object.__new__(server.TesterHandler)
        handler.server = self.httpd
        handler.headers = http.client.parse_headers(io.BytesIO(
            ("Host: " + host + "\r\nHost: " + host + "\r\n\r\n").encode()
        ))
        with self.assertRaises(server.TesterError) as raised:
            handler._guard()
        self.assertEqual(raised.exception.status, 403)

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.putrequest("POST", "/api/health")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", "2")
            conn.putheader("Content-Length", "2")
            # Invalid framing must be rejected before the caller sends a body.
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
        finally:
            conn.close()
        self.httpd.client.opener.open.assert_not_called()

    def test_static_allowlist_does_not_serve_proxy_source_or_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            for filename in ("index.html", "app.js", "style.css"):
                Path(folder, filename).write_text("fixture " + filename)
            with patch.object(server, "STATIC", Path(folder)):
                for path in ("/", "/index.html", "/app.js", "/style.css"):
                    with self.subTest(path=path):
                        status, _, raw = self.request(path)
                        self.assertEqual(status, 200)
                        self.assertTrue(raw.startswith(b"fixture "))
                for path in ("/server.py", "/../server.py", "/%2e%2e/server.py",
                             "/..%2fserver.py", "/.env", "/api/config?key=hidden",
                             "/index.html?anything=1"):
                    with self.subTest(path=path):
                        status, _, raw = self.request(path)
                        self.assertEqual(status, 404)
                        self.assertEqual(json.loads(raw)["error"], "Not found.")

    def test_content_type_transfer_encoding_and_body_limit(self):
        cases = (
            ({"Content-Type": "text/plain"}, 415),
            ({"Content-Type": "application/json", "Transfer-Encoding": "chunked"}, 400),
            ({"Content-Type": "application/json", "Content-Length": "-1"}, 400),
            ({"Content-Type": "application/json", "Content-Length": "10000001"}, 413),
        )
        for headers, expected in cases:
            with self.subTest(headers=headers):
                # Check the full advertised limit before uploading 10 MB. A
                # rejected streaming body can legitimately close the socket.
                invalid_framing = "Transfer-Encoding" in headers or "Content-Length" in headers
                body = b"" if invalid_framing else b"{}"
                status, _, _ = self.request("/api/health", body, headers=headers)
                self.assertEqual(status, expected)
        with patch.object(server, "MAX_BODY_BYTES", 1024):
            status, _, raw = self.request("/api/health", b" " * 2048,
                                          headers={"Content-Type": "application/json"})
            self.assertEqual(status, 413, raw)
        self.assertEqual(server.MAX_BODY_BYTES, 10_000_000)
        self.httpd.client.opener.open.assert_not_called()

    def test_malformed_json_and_unknown_fields_are_rejected(self):
        for body in (b"{", b"[]", b"null", b'"text"', b'{"key":NaN}',
                     b'{"key":Infinity}', b"\xff", b'{"webhook":"https://evil.example"}'):
            with self.subTest(body=body):
                status, _, raw = self.request("/api/health", body,
                                              headers={"Content-Type": "application/json"})
                self.assertEqual(status, 400, raw)
                self.assertFalse(json.loads(raw)["submission_uncertain"])
        self.httpd.client.opener.open.assert_not_called()

    def test_missing_required_request_fields_have_clear_errors(self):
        for action in ("run", "status", "cancel"):
            with self.subTest(action=action):
                status, _, raw = self.request("/api/" + action, {})
                self.assertEqual(status, 400, raw)
                self.assertTrue(json.loads(raw)["error"])
        self.httpd.endpoint_id = ""
        status, _, _ = self.request("/api/health", {})
        self.assertEqual(status, 400)
        self.httpd.endpoint_id = "configured-endpoint"
        self.httpd.client.api_key = ""
        status, _, _ = self.request("/api/health", {})
        self.assertEqual(status, 401)
        self.httpd.client.opener.open.assert_not_called()

    def test_upstream_timeout_and_generic_errors_never_echo_credentials(self):
        secret = "never-print-this-secret"
        self.httpd.client.opener.open.side_effect = TimeoutError(secret)
        status, _, raw = self.request("/api/run", {
            "api_key": secret, "input": {"mode": "text_to_video"},
        })
        self.assertEqual(status, 504)
        self.assertTrue(json.loads(raw)["submission_uncertain"])
        self.assertNotIn(secret.encode(), raw)
        self.httpd.client.opener.open.assert_called_once()
        with patch.object(self.httpd.client, "request", side_effect=RuntimeError(secret)):
            status, _, raw = self.request("/api/health", {"api_key": secret})
        self.assertEqual(status, 500)
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(b"Traceback", raw)


if __name__ == "__main__":
    unittest.main()
