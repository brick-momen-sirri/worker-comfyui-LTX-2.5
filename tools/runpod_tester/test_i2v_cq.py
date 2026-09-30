"""Experimental I2V validation and transport; no GPU jobs or live credentials."""
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
import i2v_cq
import server
from ltx_worker.errors import InputError


def request(strength=1.0):
    data = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(data, "PNG")
    return {"mode": i2v_cq.MODE, "prompt": "The camera moves forward slowly.",
            "parameters": {"cq_lora_strength": strength},
            "media": {"image": {"base64": "data:image/png;base64," + base64.b64encode(data.getvalue()).decode()}}}


class I2VCQTests(unittest.TestCase):
    def test_experiment_is_only_advertised_with_local_opt_in(self):
        with patch.dict(os.environ, {"RUNPOD_TESTER_I2V_CQ_EXPERIMENT": "0"}):
            self.assertNotIn(i2v_cq.MODE, {m["id"] for m in server.get_config(runtime="cq-v2")["modes"]})
        with patch.dict(os.environ, {"RUNPOD_TESTER_I2V_CQ_EXPERIMENT": "1"}):
            config = server.get_config(runtime="cq-v2")
            mode = next(m for m in config["modes"] if m["id"] == i2v_cq.MODE)
            self.assertEqual(mode["transport"], "workflow")
            self.assertEqual(mode["parameters"]["defaults"]["num_frames"], 49)
            self.assertTrue(mode["prompt_required"])
            self.assertNotIn(i2v_cq.MODE, {m["id"] for m in server.get_config(runtime="comfyui")["modes"]})

    def test_real_png_is_normalized_and_only_cq_strength_changes(self):
        graphs = []
        for strength in (0.0, 1.0):
            source = request(strength)
            original = copy.deepcopy(source)
            output = i2v_cq.compile_workflow(source)
            self.assertEqual(source, original)
            graph = output["workflow"]
            image = output["images"][0]
            self.assertEqual(graph["load_image"]["inputs"]["image"], image["name"])
            Image.open(io.BytesIO(base64.b64decode(image["image"]))).verify()
            self.assertEqual(graph["cq_lora"]["inputs"]["strength_model"], strength)
            self.assertEqual(graph["sample_guider"]["inputs"]["model"], ["cq_lora", 0])
            self.assertEqual(graph["video_latent"]["inputs"], dict(width=1024, height=576, length=49, batch_size=1))
            graph["load_image"]["inputs"]["image"] = "input.png"
            graph["save"]["inputs"]["filename_prefix"] = "output"
            graph["cq_lora"]["inputs"]["strength_model"] = 0.0
            graphs.append(graph)
        self.assertEqual(*graphs)

    def test_bad_media_and_unsupported_settings_fail_before_submission(self):
        for changes in ({"media": {}}, {"media": {"image": {"base64": "invalid"}}},
                        {"parameters": {"num_frames": 129}}, {"parameters": {"num_frames": 120}},
                        {"parameters": {"width": 2592}},
                        {"parameters": {"cq_lora_strength": float("nan")}}, {"prompt": ""},
                        {"workflow": {}}, {"mode": "video_enhance_cq_v2"}):
            with self.subTest(changes=changes), self.assertRaises(InputError):
                i2v_cq.compile_workflow(request() | changes)

    def test_full_hd_generates_on_aligned_canvas_and_respects_pixel_cap(self):
        source = request() | {"parameters": {"width": 1920, "height": 1088, "num_frames": 121}}
        graph = i2v_cq.compile_workflow(source)["workflow"]
        self.assertEqual(graph["video_latent"]["inputs"], dict(width=1920, height=1088, length=121, batch_size=1))
        self.assertEqual(graph["audio_latent"]["inputs"]["frames_number"], 121)
        self.assertEqual(graph["image_resize"]["inputs"]["height"], 1088)
        self.assertEqual(graph["decode_video"]["class_type"], "VAEDecode")
        with patch.dict(os.environ, {"MAX_GENERATION_FRAMES": "49"}):
            with self.assertRaisesRegex(InputError, "MAX_GENERATION_FRAMES"):
                i2v_cq.compile_workflow(source)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "1572864"}):
            with self.assertRaisesRegex(InputError, "MAX_CQ_GENERATION_PIXELS"):
                i2v_cq.compile_workflow(source)

    def test_1440p_121_frames_requires_explicit_pixel_and_frame_caps(self):
        source = request() | {"parameters": {"width": 2560, "height": 1440, "num_frames": 121}}
        with self.assertRaisesRegex(InputError, "MAX_CQ_GENERATION_PIXELS"):
            i2v_cq.compile_workflow(source)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400"}):
            with self.assertRaisesRegex(InputError, "MAX_CQ_HIGH_RES_FRAMES"):
                i2v_cq.compile_workflow(source)
            with patch.dict(os.environ, {"MAX_CQ_HIGH_RES_FRAMES": "121"}):
                graph = i2v_cq.compile_workflow(source)["workflow"]
                self.assertEqual(graph["video_latent"]["inputs"], dict(width=2560, height=1440, length=121, batch_size=1))
                self.assertEqual(graph["image_resize"]["inputs"]["width"], 2560)
                self.assertEqual(graph["image_resize"]["inputs"]["height"], 1440)
                self.assertEqual(graph["video"]["inputs"]["images"], ["decode_video", 0])
                with patch.dict(os.environ, {"MAX_CQ_OUTPUT_PIXELS": "2088960"}):
                    with self.assertRaisesRegex(InputError, "MAX_CQ_OUTPUT_PIXELS"):
                        i2v_cq.compile_workflow(source)

    def test_run_keeps_legacy_delivery_envelope_without_retry(self):
        with patch.dict(os.environ, {"RUNPOD_TESTER_I2V_CQ_EXPERIMENT": "1", "RUNPOD_TESTER_CQ_TRANSPORT": "named"}):
            client = server.RunpodClient("test-key", runtime="cq-v2")
            client.opener = Mock()
            client.opener.open.return_value = io.BytesIO(b'{"id":"i2v-job","status":"IN_QUEUE"}')
            response = client.request("run", "endpoint", job_input=request())
            self.assertEqual(response["id"], "i2v-job")
            envelope = json.loads(client.opener.open.call_args.args[0].data)
            self.assertEqual(set(envelope["input"]), {"workflow", "images"})
            self.assertEqual(envelope["policy"]["executionTimeout"], 3600000)
            client.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
