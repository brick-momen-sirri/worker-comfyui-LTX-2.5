import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ltx_worker.adapter import execute, prepare_input, _remove_job_directory
from ltx_worker.errors import InputError
from tests.test_media import encoded_image


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.spec = {"file": "fake.json", "defaults": {"prompt": "", "negative_prompt": "", "width": 768, "height": 512, "num_frames": 9, "fps": 24, "seed": 42, "cfg": 1.0},
                     "constraints": {"width": {"type": "integer", "minimum": 256, "maximum": 1536, "multiple_of": 32}, "num_frames": {"type": "integer", "minimum": 9, "maximum": 241, "multiple_of": 8, "offset": 1}, "cfg": {"type": "number", "enum": [1.0]}},
                     "bindings": {"prompt": [{"node_id": "1", "input": "text"}], "seed": [{"node_id": "2", "input": "seed"}]},
                     "media": {"first_frame": {"node_id": "3", "input": "image", "kind": "image", "required": True}, "last_frame": {"node_id": "4", "input": "image", "kind": "image", "required": True}},
                     "output_prefix": [{"node_id": "5", "input": "filename_prefix"}]}
        (self.root / "fake.json").write_text(json.dumps({str(n): {"class_type": "fake", "inputs": {}} for n in range(1, 6)}))
        for target, value in (("ltx_worker.adapter.WORKFLOWS", self.root), ("ltx_worker.adapter.load_manifest", lambda: {"modes": {"first_last_frame": self.spec}})):
            active = patch(target, value)
            active.start()
            self.addCleanup(active.stop)
        env = patch.dict(os.environ, {"COMFY_INPUT_DIR": str(self.root / "input"), "COMFY_OUTPUT_DIR": str(self.root / "output")})
        env.start()
        self.addCleanup(env.stop)
        self.payload = {"mode": "first_last_frame", "prompt": "A tree sways", "media": {"first_frame": encoded_image(), "last_frame": "data:image/png;base64," + encoded_image()}}

    def test_preserves_job_id_binds_independent_media_and_cleans_after_delivery(self):
        seen = []
        def legacy(job, **kwargs):
            seen.append(job)
            self.assertEqual(job["id"], "original-id")
            self.assertFalse(kwargs["scan_text_artifacts"])
            graph = job["input"]["workflow"]
            self.assertEqual(graph["1"]["inputs"]["text"], "A tree sways")
            for n in ("3", "4"):
                self.assertTrue((self.root / "input" / graph[n]["inputs"]["image"]).is_file())
            out = self.root / "output" / graph["5"]["inputs"]["filename_prefix"]
            out.parent.mkdir(parents=True)
            out.with_suffix(".mp4").write_bytes(b"delivered artifact")
            return {"success": True, "prompt_id": "comfy-id", "videos": [{"filename": "a.mp4", "type": "s3_url", "data": "https://s3.example/a"}], "credit_usage": {}}
        result = execute({"id": "original-id", "input": self.payload, "webhook": "https://caller.example/hook"}, legacy)
        self.assertTrue(result["success"])
        self.assertEqual(seen[0]["webhook"], "https://caller.example/hook")
        self.assertFalse(list((self.root / "input" / "ltx_jobs").iterdir()))
        self.assertFalse(list((self.root / "output" / "ltx_jobs").iterdir()))

    def test_failure_retains_inputs_and_requests_worker_recycle(self):
        result = execute({"id": "job", "input": self.payload}, lambda *args, **kwargs: {"error": "upload failed"})
        self.assertEqual(result["error"], "upload failed")
        self.assertTrue(result["refresh_worker"])
        self.assertTrue(list((self.root / "input").rglob("*.png")))

    def test_success_without_video_artifact_fails(self):
        result = execute({"id": "job", "input": self.payload}, lambda *args, **kwargs: {"success": True, "status": "success_no_outputs", "videos": []})
        self.assertNotIn("success", result)
        self.assertEqual(result["error_code"], "MISSING_OUTPUT")
        self.assertTrue(result["refresh_worker"])

    def test_large_response_fails_without_inline_blob(self):
        with patch.dict(os.environ, {"MAX_RESULT_BYTES": "20"}):
            result = execute({"id": "job", "input": self.payload}, lambda *args, **kwargs: {"success": True, "videos": [{"data": "x" * 100}]})
        self.assertEqual(result["error_code"], "RESULT_TOO_LARGE")
        self.assertNotIn("videos", result)

    def test_invalid_media_cleans_partial_input_and_never_queues(self):
        payload = copy.deepcopy(self.payload)
        payload["media"]["last_frame"] = "invalid!!"
        def legacy(*args, **kwargs):
            self.fail("must not queue invalid media")
        result = execute({"id": "job", "input": payload}, legacy)
        self.assertEqual(result["error_code"], "INVALID_MEDIA")
        self.assertFalse(list((self.root / "input").rglob("*.png")))

    def test_settings_missing_media_and_workflow_conflict(self):
        for change in ({"parameters": {"width": 769}}, {"parameters": {"num_frames": 10}}, {"parameters": {"seed": True}}, {"parameters": {"cfg": 7}}, {"parameters": {"steps": 20}}, {"media": {}}, {"workflow": {}}):
            with self.subTest(change=change), self.assertRaises(InputError):
                prepare_input(dict(self.payload, **change))

    def test_raw_workflow_compatibility_and_optional_disable(self):
        job = {"id": "id", "input": {"workflow": {"x": "original"}, "images": [{"name": "a", "image": "payload"}]}}
        received = []
        result = execute(job, lambda value: received.append(value) or {"success": True})
        self.assertTrue(result["success"])
        self.assertIs(received[0], job)
        with patch.dict(os.environ, {"ALLOW_CUSTOM_WORKFLOWS": "false"}):
            self.assertEqual(execute(job, lambda _: self.fail())["error_code"], "UNSUPPORTED_MODE")

    def test_cleanup_refuses_nonjob_paths(self):
        with self.assertRaises(RuntimeError):
            _remove_job_directory(self.root, self.root)
        with self.assertRaises(RuntimeError):
            _remove_job_directory(self.root, self.root / "user-folder")


if __name__ == "__main__":
    unittest.main()
