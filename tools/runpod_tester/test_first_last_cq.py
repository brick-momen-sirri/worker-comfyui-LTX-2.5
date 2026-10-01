"""Endpoint-image conditioning, media isolation, and CQ transport admission."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
import server
import first_last_cq
from ltx_worker.errors import InputError


def encoded(color):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def request():
    return {"mode": first_last_cq.MODE, "prompt": "The camera smoothly pulls back along the building.",
            "media": {"first_frame": {"base64": "data:image/png;base64," + encoded("red")},
                      "last_frame": {"base64": encoded("blue")}}}


class FirstLastCQTests(unittest.TestCase):
    def test_two_distinct_images_condition_both_ends_and_crop_guides(self):
        source = request()
        original = copy.deepcopy(source)
        compiled = first_last_cq.compile_workflow(source)
        self.assertEqual(source, original)
        graph, images = compiled["workflow"], compiled["images"]
        self.assertEqual(len(images), 2)
        self.assertNotEqual(images[0]["name"], images[1]["name"])
        for role, item, color in zip(("first_frame", "last_frame"), images, ((255, 0, 0), (0, 0, 255))):
            self.assertEqual(graph[f"load_{role}"]["inputs"]["image"], item["name"])
            self.assertEqual(Image.open(io.BytesIO(base64.b64decode(item["image"]))).getpixel((0, 0)), color)
        self.assertEqual(graph["first_frame_condition"]["inputs"]["frame_idx"], 0)
        self.assertEqual(graph["last_frame_condition"]["inputs"]["frame_idx"], -1)
        self.assertEqual(graph["last_frame_condition"]["inputs"]["positive"], ["first_frame_condition", 0])
        self.assertEqual(graph["sample_guider"]["inputs"]["positive"], ["last_frame_condition", 0])
        self.assertEqual(graph["sample_guider"]["inputs"]["model"], ["cq_lora", 0])
        self.assertEqual(graph["decode_video"]["inputs"]["samples"], ["sample_crop", 2])

    def test_each_role_accepts_its_own_media_format(self):
        source = request()
        source["media"]["last_frame"] = {"url": "https://storage.example.invalid/last.png"}
        import ltx_worker.media as media_module
        original = media_module.ingest
        seen = {}
        def fake_download(value, kind, directory, role, params):
            seen[role] = value
            value = {"base64": encoded("blue")} if "url" in value else value
            return original(value, kind, directory, role, params)
        with patch.object(media_module, "ingest", side_effect=fake_download):
            result = first_last_cq.compile_workflow(source)
        self.assertIn("base64", seen["first_frame"])
        self.assertIn("url", seen["last_frame"])
        self.assertEqual(len(result["images"]), 2)

    def test_missing_endpoints_and_invalid_settings_rejected(self):
        for changes in ({"media": {"first_frame": request()["media"]["first_frame"]}},
                        {"media": {"last_frame": request()["media"]["last_frame"]}},
                        {"parameters": {"num_frames": 120}}, {"parameters": {"fps": 30}},
                        {"parameters": {"width": 1280, "height": 720}},
                        {"parameters": {"last_frame_strength": float("nan")}},
                        {"prompt": ""}):
            with self.subTest(changes=changes), self.assertRaises(InputError):
                first_last_cq.compile_workflow(request() | changes)

    def test_qhd_updates_both_images_and_obeys_pixel_frame_caps(self):
        source = request() | {"parameters": {"width": 2560, "height": 1440, "num_frames": 121}}
        with self.assertRaises(InputError):
            first_last_cq.compile_workflow(source)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400", "MAX_CQ_HIGH_RES_FRAMES": "121"}):
            graph = first_last_cq.compile_workflow(source)["workflow"]
            for node in ("video_latent", "first_frame_resize", "last_frame_resize"):
                self.assertEqual((graph[node]["inputs"]["width"], graph[node]["inputs"]["height"]), (2560, 1440))
            self.assertEqual(graph["video_latent"]["inputs"]["length"], 121)
            self.assertEqual(graph["audio_latent"]["inputs"]["frames_number"], 121)
            with patch.dict(os.environ, {"MAX_CQ_HIGH_RES_FRAMES": "25"}):
                with self.assertRaises(InputError):
                    first_last_cq.compile_workflow(source)

    def test_opt_in_is_separate_and_disabled_requests_never_submit(self):
        with patch.dict(os.environ, {"RUNPOD_TESTER_FIRST_LAST_CQ_EXPERIMENT": "0"}):
            client = server.RunpodClient("test-key", runtime="cq-v2")
            client.opener = Mock()
            with self.assertRaises(server.TesterError):
                client.request("run", "endpoint", job_input=request())
            client.opener.open.assert_not_called()
        with patch.dict(os.environ, {"RUNPOD_TESTER_FIRST_LAST_CQ_EXPERIMENT": "1"}):
            mode = next(m for m in server.get_config(runtime="cq-v2")["modes"] if m["id"] == first_last_cq.MODE)
            self.assertEqual(mode["transport"], "workflow")
            self.assertEqual({item["role"] for item in mode["media"]}, {"first_frame", "last_frame"})

    def test_transport_preserves_job_contract_and_does_not_retry(self):
        with patch.dict(os.environ, {"RUNPOD_TESTER_FIRST_LAST_CQ_EXPERIMENT": "1", "RUNPOD_TESTER_CQ_TRANSPORT": "named"}):
            client = server.RunpodClient("test-key", runtime="cq-v2")
            client.opener = Mock()
            client.opener.open.return_value = io.BytesIO(b'{"id":"first-last-job","status":"IN_QUEUE"}')
            result = client.request("run", "endpoint", job_input=request())
            envelope = json.loads(client.opener.open.call_args.args[0].data)
            self.assertEqual(result["id"], "first-last-job")
            self.assertEqual(len(envelope["input"]["images"]), 2)
            self.assertEqual(set(envelope["input"]), {"workflow", "images"})
            self.assertEqual(envelope["policy"]["executionTimeout"], 3600000)
            client.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
