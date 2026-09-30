"""4K compatibility transport tests; no network calls or GPU work."""
import copy
import io
import json
import os
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import four_k
import server


def request(mode="image_to_video_4k"):
    role = "image" if mode.startswith("image") else "video"
    return {"mode": mode, "prompt": "Preserve this scene", "media": {
        role: {"url": "https://storage.example.com/source?signature=private"}}}


class FourKTransportTests(unittest.TestCase):
    def test_compiler_binds_only_bundled_graph_and_isolates_input_and_output_names(self):
        source = request()
        before = copy.deepcopy(source)
        first = four_k.compile_workflow(source)
        second = four_k.compile_workflow(source)
        self.assertEqual(source, before)
        self.assertNotIn("mode", first)
        self.assertEqual(first["workflow"]["positive"]["inputs"]["text"], source["prompt"])
        self.assertEqual(first["images"][0]["url"], source["media"]["image"]["url"])
        self.assertEqual(first["workflow"]["load_image"]["inputs"]["image"], first["images"][0]["name"])
        self.assertNotEqual(first["images"][0]["name"], second["images"][0]["name"])
        self.assertNotEqual(first["workflow"]["save"]["inputs"]["filename_prefix"],
                            second["workflow"]["save"]["inputs"]["filename_prefix"])
        self.assertEqual(first["workflow"]["pixel_video_latent"]["inputs"]["width"], 3840)

    def test_invalid_settings_and_caller_graphs_are_rejected(self):
        for change in ({"workflow": {}}, {"parameters": {"num_frames": 121}},
                       {"parameters": {"width": 1024}}, {"mode": "image_to_video"}):
            with self.subTest(change=change), self.assertRaises(four_k.InputError):
                four_k.compile_workflow(dict(request(), **change))
        for value in ({"url": "https://s.example/a", "base64": "eA=="},
                      "http://s.example/a", "https://user:secret@s.example/a", "not-base64"):
            with self.subTest(value=value), self.assertRaises(four_k.InputError):
                four_k.media_payload(value)

    def test_native_and_compatibility_transport_preserve_runpod_envelope(self):
        for transport in ("workflow", "named"):
            with self.subTest(transport=transport), patch.dict(os.environ, {"RUNPOD_TESTER_4K_TRANSPORT": transport}):
                client = server.RunpodClient("test-key")
                client.opener = Mock()
                client.opener.open.return_value = io.BytesIO(b'{"id":"job-4k","status":"IN_QUEUE"}')
                result = client.request("run", "endpoint", job_input=request())
                self.assertEqual(result["id"], "job-4k")
                self.assertNotIn("policy", json.loads(client.opener.open.call_args.args[0].data))
                sent = json.loads(client.opener.open.call_args.args[0].data)["input"]
                self.assertEqual("workflow" in sent, transport == "workflow")
                self.assertEqual("mode" in sent, transport == "named")
                client.opener.open.assert_called_once()

    def test_key_retention_is_opt_in_and_only_after_successful_health(self):
        for keep in (False, True):
            client = server.RunpodClient(keep_key_in_memory=keep)
            client.opener = Mock()
            client.opener.open.return_value = io.BytesIO(b'{"workers":{"ready":1}}')
            client.request("health", "endpoint", api_key="test-key")
            self.assertEqual(client.api_key, "test-key" if keep else "")

    def test_direct_4k_config_and_transport_keep_full_resolution_and_one_pass(self):
        source = request("image_to_video_native_4k")
        source["parameters"] = {"num_frames": 121}
        config = next(mode for mode in server.get_config()["modes"] if mode["id"] == source["mode"])
        self.assertEqual(config["parameters"]["defaults"]["width"], 3840)
        self.assertEqual(config["parameters"]["defaults"]["height"], 2176)
        self.assertEqual(config["parameters"]["defaults"]["num_frames"], 9)
        client = server.RunpodClient("test-key")
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"id":"native-4k","status":"IN_QUEUE"}')
        with patch.dict(os.environ, {"RUNPOD_TESTER_4K_TRANSPORT": "workflow"}):
            client.request("run", "endpoint", job_input=source)
        envelope = json.loads(client.opener.open.call_args.args[0].data)
        self.assertEqual(envelope["policy"], {"executionTimeout": 3600000, "ttl": 7200000})
        sent = envelope["input"]
        graph = sent["workflow"]
        self.assertEqual(graph["video_latent"]["inputs"]["width"], 3840)
        self.assertEqual(graph["video_latent"]["inputs"]["height"], 2176)
        self.assertEqual(graph["video_latent"]["inputs"]["length"], 121)
        self.assertEqual(graph["audio_latent"]["inputs"]["frames_number"], 121)
        self.assertEqual(sum(node["class_type"] == "SamplerCustomAdvanced" for node in graph.values()), 1)
        self.assertFalse(any("Lora" in node["class_type"] or "Upscale" in node["class_type"] for node in graph.values()))
        self.assertIn("LTX25_NATIVE_4K", graph["save"]["inputs"]["filename_prefix"])
        self.assertEqual(sent["images"][0]["url"], source["media"]["image"]["url"])
        client.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
