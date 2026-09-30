"""CQ Full HD admission and legacy transport tests; no GPU/network inference."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import server
import cq_transport
from ltx_worker.adapter import prepare_input
from ltx_worker.errors import InputError


def request(**parameters):
    return {"mode": "video_enhance_cq_v2", "parameters": dict(
        width=1920, height=1088, num_frames=33) | parameters,
        "media": {"video": {"base64": "dmlkZW8="}}}


class CQTransportTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((cq_transport.ROOT / "workflows/manifest.cq-v2.json").read_text())

    def test_aligned_full_hd_is_admitted_and_caps_still_apply(self):
        prepare_input(request(), manifest=self.manifest)
        for key, value in (("width", 1952), ("height", 1080), ("height", 1920)):
            source = request()
            source["parameters"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(InputError):
                prepare_input(source, manifest=self.manifest)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "1572864"}):
            with self.assertRaisesRegex(InputError, "MAX_CQ_GENERATION_PIXELS"):
                prepare_input(request(), manifest=self.manifest)

    def test_normalization_and_latents_receive_actual_full_hd_dimensions(self):
        source = request()
        original = copy.deepcopy(source)
        def normalize(media, directory, params):
            self.assertEqual((params["width"], params["height"], params["num_frames"]), (1920, 1088, 33))
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"normalized-media")
            self.temporary_path = path
            return path
        with patch.object(cq_transport, "normalize_video", side_effect=normalize):
            compiled = cq_transport.compile_workflow(source)
        self.assertEqual(source, original)
        self.assertFalse(self.temporary_path.exists())
        graph = compiled["workflow"]
        self.assertEqual(graph["video_latent"]["inputs"], dict(width=1920, height=1088, length=33, batch_size=1))
        self.assertEqual(graph["audio_latent"]["inputs"]["frames_number"], 33)
        self.assertEqual(graph["load_video"]["inputs"]["file"], compiled["videos"][0]["name"])
        self.assertEqual(base64.b64decode(compiled["videos"][0]["url"]), b"normalized-media")
        self.assertFalse(any("Upscale" in node["class_type"] for node in graph.values()))

    def test_1440p_requires_opt_in_and_short_sections(self):
        source = request(width=2560, height=1440, num_frames=25)
        with self.assertRaisesRegex(InputError, "MAX_CQ_GENERATION_PIXELS"):
            prepare_input(source, manifest=self.manifest)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400"}):
            with self.assertRaisesRegex(InputError, "MAX_CQ_OUTPUT_PIXELS"):
                prepare_input(source, manifest=self.manifest)
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400"}):
            prepare_input(source, manifest=self.manifest)
            with self.assertRaisesRegex(InputError, "25 frames"):
                prepare_input(request(width=2560, height=1440, num_frames=33), manifest=self.manifest)
            with self.assertRaises(InputError):
                prepare_input(request(width=2592, height=1440, num_frames=25), manifest=self.manifest)

    def test_long_1440p_requires_explicit_frame_cap(self):
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400", "MAX_CQ_HIGH_RES_FRAMES": "121"}):
            prepare_input(request(width=2560, height=1440, num_frames=121), manifest=self.manifest)
            with self.assertRaisesRegex(InputError, "MAX_CQ_HIGH_RES_FRAMES"):
                prepare_input(request(width=2560, height=1440, num_frames=129), manifest=self.manifest)
            with patch.dict(os.environ, {"MAX_CQ_HIGH_RES_FRAMES": "1000"}):
                with self.assertRaises(InputError):
                    prepare_input(request(width=2560, height=1440, num_frames=161), manifest=self.manifest)

    def test_1440p_graph_runs_at_target_size_without_output_resize(self):
        def normalize(media, directory, params):
            self.assertEqual((params["width"], params["height"]), (2560, 1440))
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"normalized")
            return path
        with patch.dict(os.environ, {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400"}), patch.object(cq_transport, "normalize_video", side_effect=normalize):
            graph = cq_transport.compile_workflow(request(width=2560, height=1440, num_frames=25))["workflow"]
        self.assertEqual(graph["video_latent"]["inputs"], dict(width=2560, height=1440, length=25, batch_size=1))
        self.assertFalse(any("Upscale" in node["class_type"] or "Scale" in node["class_type"] for node in graph.values()))

    def test_arbitrary_graphs_and_missing_media_are_rejected(self):
        for source in (dict(request(), workflow={}), dict(request(), media={}), dict(request(), mode="text_to_video")):
            with self.assertRaises(InputError):
                cq_transport.compile_workflow(source)

    def test_transport_preserves_envelope_and_does_not_retry(self):
        for transport in ("named", "workflow"):
            with self.subTest(transport=transport), patch.dict(os.environ, {"RUNPOD_TESTER_CQ_TRANSPORT": transport}), patch.object(cq_transport, "compile_workflow", return_value={"workflow": {"known": {}}, "videos": []}):
                client = server.RunpodClient("test-key", runtime="cq-v2")
                client.opener = Mock()
                client.opener.open.return_value = io.BytesIO(b'{"id":"cq-job","status":"IN_QUEUE"}')
                result = client.request("run", "endpoint", job_input=request())
                self.assertEqual(result["id"], "cq-job")
                sent = json.loads(client.opener.open.call_args.args[0].data)
                self.assertEqual(sent["policy"], {"executionTimeout": 3600000, "ttl": 7200000})
                self.assertEqual("workflow" in sent["input"], transport == "workflow")
                client.opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
