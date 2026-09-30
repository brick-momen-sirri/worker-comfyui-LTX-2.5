"""Exercise every shipped mode's adapter bindings without claiming GPU execution."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ltx_worker.adapter import execute, load_manifest, prepare_input
from ltx_worker.errors import InputError


class ShippedContractTests(unittest.TestCase):
    def test_every_mode_compiles_parameters_media_and_output_contract(self):
        manifest = load_manifest()
        for mode, spec in manifest["modes"].items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                payload = {"mode": mode, "prompt": "Adapter test prompt", "parameters": {"seed": 12345}, "media": {role: "placeholder" for role in spec["media"]}}
                if "tracks_json" in spec["defaults"]:
                    payload["parameters"]["tracks_json"] = spec["defaults"]["tracks_json"]
                def ingest(source, kind, directory, role, params, **kwargs):
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / (role + {"image": ".png", "video": ".mp4", "audio": ".wav"}[kind])
                    path.write_bytes(b"mock-decoded")
                    return path
                def legacy(job, **kwargs):
                    graph = job["input"]["workflow"]
                    for binding in spec["bindings"]["seed"]:
                        self.assertEqual(graph[binding["node_id"]]["inputs"][binding["input"]], 12345)
                    for binding in spec["bindings"]["prompt"]:
                        self.assertEqual(graph[binding["node_id"]]["inputs"][binding["input"]], "Adapter test prompt")
                    for dimension in ("width", "height"):
                        for binding in spec["bindings"].get(dimension, []):
                            expected = spec["defaults"][dimension] * binding.get("multiplier", 1)
                            self.assertEqual(graph[binding["node_id"]]["inputs"][binding["input"]], expected)
                    for role, binding in spec["media"].items():
                        file = graph[binding["node_id"]]["inputs"][binding["input"]]
                        self.assertTrue((root / "input" / file).is_file())
                    for binding in spec["output_prefix"]:
                        self.assertTrue(graph[binding["node_id"]]["inputs"][binding["input"]].startswith("ltx_jobs/"))
                    kind = "audio" if spec["output_kind"] == "audio" else "videos"
                    return {"success": True, kind: [{"filename": "result", "type": "base64", "data": "eA=="}]}
                with patch.dict("os.environ", {"COMFY_INPUT_DIR": str(root / "input"), "COMFY_OUTPUT_DIR": str(root / "output")}), patch("ltx_worker.adapter.ingest", side_effect=ingest):
                    result = execute({"id": "runpod-id", "input": payload}, legacy)
                self.assertTrue(result.get("success"), result)

    def test_exact_distilled_schedule_and_model_generation(self):
        base = Path(__file__).resolve().parents[1] / "workflows"
        for mode, spec in load_manifest()["modes"].items():
            graph = json.loads((base / spec["file"]).read_text())
            model = graph["model"]["inputs"]["unet_name"]
            self.assertIn("ltx-2.5-22b-distilled", model)
            self.assertIn("comfy-int8-convrot", model)
            self.assertIn("comfy-int8-convrot", graph["clip"]["inputs"]["clip_name"])
            self.assertEqual(graph["model"]["inputs"]["weight_dtype"], "default")
            for node in graph.values():
                if node["class_type"] == "ManualSigmas" and node["inputs"]["sigmas"].startswith("1.0,"):
                    self.assertEqual(len(node["inputs"]["sigmas"].split(",")), 9)

    def test_integer_overflow_and_invalid_settings_are_caller_errors(self):
        for change in ({"seed": 10**500}, {"num_frames": 10}, {"width": 1080}, {"steps": 50}, {"cfg": float("nan")}):
            with self.subTest(change=list(change)), self.assertRaises(InputError):
                prepare_input({"mode": "text_to_video", "prompt": "Test", "parameters": change})

    def test_checked_in_request_examples_parse(self):
        examples = Path(__file__).resolve().parents[1] / "examples"
        for path in examples.glob("*.json"):
            from ltx_worker.dfr import DFR_MODES, validate_input as validate_dfr
            job_input = json.loads(path.read_text())["input"]
            if job_input.get("mode") in DFR_MODES:
                validate_dfr(job_input)
            elif job_input.get("mode") == "video_enhance_cq_v2":
                cq = json.loads((Path(__file__).resolve().parents[1] / "workflows/manifest.cq-v2.json").read_text())
                # These examples explicitly require the opt-in environment in
                # docs/cq-v2.md. Keep ordinary examples under default admission.
                environment = {}
                if path.name in ("video_enhance_cq_v2_2560.json", "video_enhance_cq_v2_2560_121.json"):
                    environment = {"MAX_CQ_GENERATION_PIXELS": "3686400", "MAX_CQ_OUTPUT_PIXELS": "3686400"}
                if path.name == "video_enhance_cq_v2_2560_121.json":
                    environment["MAX_CQ_HIGH_RES_FRAMES"] = "121"
                with self.subTest(example=path.name), patch.dict("os.environ", environment), patch("ltx_worker.adapter.load_manifest", return_value=cq):
                    prepare_input(job_input)
            else:
                prepare_input(job_input)

    def test_cq_v2_contract_uses_publisher_recipe_and_optional_prompt(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "workflows/manifest.cq-v2.json").read_text())
        with patch("ltx_worker.adapter.load_manifest", return_value=manifest):
            spec, params, media, graph = prepare_input({
                "mode": "video_enhance_cq_v2",
                "media": {"video": {"url": "https://bucket.example/source.mp4"}},
            })
        self.assertFalse(spec["prompt_required"])
        self.assertEqual(params["prompt"], "")
        self.assertEqual((params["fps"], params["num_frames"]), (30, 153))
        self.assertEqual(set(media), {"video"})
        self.assertEqual(graph["model"]["inputs"]["unet_name"],
                         "ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors")
        self.assertEqual(graph["video_vae"]["inputs"]["vae_name"],
                         "ltx-2.5-video-vae-conv-bf16.safetensors")
        self.assertEqual(graph["distilled_lora"]["inputs"]["strength_model"], 0.5)
        self.assertEqual(graph["cq_lora"]["inputs"]["lora_name"],
                         "ltx2.5-CQ-enhancer-lora-V2.safetensors")
        self.assertEqual(graph["cq_lora"]["inputs"]["strength_model"], 1.0)
        self.assertEqual(len(graph["sigmas"]["inputs"]["sigmas"].split(",")), 9)
        self.assertEqual(graph["guide"]["inputs"]["strength"], 1.0)
        self.assertEqual(graph["video"]["inputs"]["audio"], ["video_components", 1])

    def test_upscaler_uses_exact_25_lora_factor_and_audio_policy(self):
        spec, params, _, graph = prepare_input({"mode": "video_upscale_x2", "prompt": "Resolve detail", "media": {"video": "placeholder"}})
        self.assertEqual(spec["output_scale"], 2)
        self.assertTrue(spec["media"]["video"]["ensure_audio"])
        self.assertTrue(any(node["inputs"].get("lora_name") == "ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors" for node in graph.values()))
        self.assertTrue(any(node["inputs"].get("latent_downscale_factor") == 2 for node in graph.values()))
        self.assertEqual(spec["defaults"]["width"], 512)
        self.assertEqual(spec["defaults"]["height"], 288)

    def test_missing_prompt_and_excessive_duration_are_rejected(self):
        with self.assertRaises(InputError):
            prepare_input({"mode": "text_to_video"})
        with self.assertRaises(InputError) as error:
            prepare_input({"mode": "text_to_video", "prompt": "Test", "parameters": {"num_frames": 241, "fps": 1}})
        self.assertIn("duration", str(error.exception))


if __name__ == "__main__":
    unittest.main()
