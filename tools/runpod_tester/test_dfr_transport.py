"""DFR runtime isolation and named transport; all cloud requests are mocked."""

import copy
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import server


MODES = {"image_to_video_dfr_4k", "text_to_video_dfr_4k"}


def request(mode="image_to_video_dfr_4k", **parameters):
    return {"mode": mode, "prompt": "A quiet scene with natural motion.",
            "parameters": parameters,
            "media": {"image": {"url": "https://storage.example.com/frame.png?signature=private"}}
            if mode.startswith("image") else {}}


def client(runtime="dfr"):
    result = server.RunpodClient("test-secret", runtime=runtime)
    result.opener = Mock()
    result.opener.open.return_value = io.BytesIO(b'{"id":"qualification-job","status":"IN_QUEUE"}')
    return result


class DFRConfigTests(unittest.TestCase):
    def test_runtime_catalogs_are_disjoint_and_comfy_remains_default(self):
        comfy = server.get_config()
        dfr = server.get_config("dfr-endpoint", True, runtime="dfr")
        self.assertEqual(comfy["runtime"], "comfyui")
        self.assertEqual(dfr["runtime"], "dfr")
        self.assertEqual({mode["id"] for mode in dfr["modes"]}, MODES)
        self.assertFalse(MODES & {mode["id"] for mode in comfy["modes"]})
        self.assertEqual(dfr["endpoint_id"], "dfr-endpoint")
        self.assertIs(dfr["api_key_configured"], True)
        self.assertNotIn("api_key", dfr)
        self.assertFalse(dfr["validation_preset_available"])
        self.assertTrue(comfy["validation_preset_available"])
        self.assertIn("separate DFR worker image", dfr["runtime_description"])

    def test_dfr_defaults_match_real_worker_validator(self):
        for mode in server.get_config(runtime="dfr")["modes"]:
            with self.subTest(mode=mode["id"]):
                defaults = copy.deepcopy(mode["parameters"]["defaults"])
                payload = request(mode["id"])
                payload["prompt"] = defaults.pop("prompt")
                payload["parameters"] = defaults
                normalized = server.dfr_module().validate_input(payload)
                self.assertEqual(normalized["parameters"], defaults)
                self.assertEqual((defaults["width"], defaults["height"], defaults["num_frames"], defaults["fps"]),
                                 (3840, 2176, 33, 24))
                self.assertEqual(mode["parameters"]["rules"]["num_frames"]["enum"], [9, 33, 121])
                self.assertEqual(mode["transport"], "named")
                self.assertEqual(mode["output_dimensions"], {"width": 3840, "height": 2160})
                self.assertNotIn("negative_prompt", mode["parameters"]["rules"])
                if mode["id"].startswith("image"):
                    self.assertEqual(defaults["image_strength"], 0.8)
                    self.assertEqual(mode["media"], [{"role": "image", "kind": "image", "required": True}])
                else:
                    self.assertEqual(mode["media"], [])

    def test_dfr_configuration_does_not_import_model_or_image_dependencies(self):
        code = """import builtins, sys
sys.path.insert(0, 'tools/runpod_tester')
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'torch', 'PIL', 'ltx_pipelines', 'runpod'}:
        raise AssertionError('Heavy dependency imported by local configuration: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import server
assert server.get_config(runtime='dfr')['runtime'] == 'dfr'
print('stdlib configuration passed')
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=server.ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "stdlib configuration passed")

    def test_invalid_runtime_rejected_before_socket_creation(self):
        for runtime in ("automatic", "", None, [], {}):
            with self.subTest(runtime=runtime), patch.object(server, "ThreadingHTTPServer") as httpd:
                with self.assertRaises(server.TesterError):
                    server.make_server(runtime=runtime)
                httpd.assert_not_called()
        with self.assertRaises(server.TesterError):
            server.RunpodClient(runtime="automatic")


class DFRTransportTests(unittest.TestCase):
    def test_wrong_runtime_cannot_submit_either_family(self):
        for runtime, payload in (("comfyui", request()),
                                 ("dfr", {"mode": "text_to_video"}),
                                 ("dfr", {"mode": "image_to_video_4k"}),
                                 ("dfr", {"mode": "image_to_video_native_4k"})):
            with self.subTest(runtime=runtime, mode=payload["mode"]):
                api = client(runtime)
                with self.assertRaisesRegex(server.TesterError, "runtime"):
                    api.request("run", "endpoint", job_input=payload)
                api.opener.open.assert_not_called()

    def test_dfr_is_named_even_when_comfy_bridge_is_configured(self):
        for mode in sorted(MODES):
            for frames in (9, 33, 121):
                with self.subTest(mode=mode, frames=frames), \
                        patch.dict(os.environ, {"RUNPOD_TESTER_4K_TRANSPORT": "workflow"}), \
                        patch.object(server.four_k, "compile_workflow") as compile_graph:
                    api = client()
                    payload = request(mode, num_frames=frames, seed=12345)
                    before = copy.deepcopy(payload)
                    result = api.request("run", "dfr-endpoint", job_input=payload)
                    self.assertEqual(payload, before)
                    self.assertEqual(result["id"], "qualification-job")
                    sent = api.opener.open.call_args.args[0]
                    self.assertEqual(sent.full_url, "https://api.runpod.ai/v2/dfr-endpoint/run")
                    self.assertEqual(sent.get_method(), "POST")
                    envelope = json.loads(sent.data)
                    self.assertEqual(envelope["policy"], {"executionTimeout": 5400000, "ttl": 7200000})
                    self.assertEqual(envelope["input"], server.dfr_module().validate_input(payload))
                    self.assertEqual(envelope["input"]["parameters"]["num_frames"], frames)
                    self.assertEqual(envelope["input"]["parameters"]["seed"], 12345)
                    self.assertNotIn("workflow", envelope["input"])
                    self.assertNotIn(b"test-secret", sent.data)
                    api.opener.open.assert_called_once()
                    compile_graph.assert_not_called()

    def test_dfr_invalid_settings_and_missing_media_never_reach_cloud(self):
        invalid = [dict(request(), media={}), dict(request(), negative_prompt="unused"),
                   dict(request(), workflow={}), dict(request(), parameters=[]),
                   dict(request(), prompt=""), request(width=1920), request(height=2160),
                   request(fps=30), request(num_frames=17), request(seed=-1), request(seed=2**32)]
        for payload in invalid:
            with self.subTest(payload=payload):
                api = client()
                with self.assertRaises(server.TesterError):
                    api.request("run", "endpoint", job_input=payload)
                api.opener.open.assert_not_called()

    def test_media_transports_are_preserved_by_named_validation(self):
        for media in ({"url": "https://storage.example.com/frame.png?X-Amz-Signature=private"},
                      "data:image/png;base64,iVBORw0KGgo=", {"base64": "iVBORw0KGgo="}):
            api = client()
            payload = request()
            payload["media"]["image"] = media
            api.request("run", "endpoint", job_input=payload)
            sent = json.loads(api.opener.open.call_args.args[0].data)
            self.assertEqual(sent["input"]["media"]["image"], media)

    def test_dfr_timeout_is_not_retried_and_does_not_leak_key(self):
        api = client()
        api.opener.open.side_effect = TimeoutError("test-secret")
        with self.assertRaises(server.TesterError) as raised:
            api.request("run", "endpoint", job_input=request())
        self.assertTrue(raised.exception.submission_uncertain)
        self.assertNotIn("test-secret", str(raised.exception))
        api.opener.open.assert_called_once()


class DFRHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server(endpoint_id="dfr-endpoint", api_key="http-test-secret", runtime="dfr")
        cls.httpd.client.opener = Mock()
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def exchange(self, path, document=None):
        connection = http.client.HTTPConnection(*self.httpd.server_address, timeout=3)
        try:
            body = None if document is None else json.dumps(document).encode()
            connection.request("GET" if body is None else "POST", path, body=body,
                               headers={} if body is None else {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_config_reports_server_runtime_without_auth_or_network(self):
        self.httpd.client.opener.reset_mock()
        status, raw = self.exchange("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["runtime"], "dfr")
        self.assertNotIn(b"http-test-secret", raw)
        self.httpd.client.opener.open.assert_not_called()

    def test_browser_cannot_override_runtime_or_route_comfy_to_dfr(self):
        for body in ({"input": request(), "runtime": "comfyui"},
                     {"input": {"mode": "text_to_video"}}):
            with self.subTest(body=body):
                self.httpd.client.opener.reset_mock()
                status, _ = self.exchange("/api/run", body)
                self.assertEqual(status, 400)
                self.httpd.client.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
