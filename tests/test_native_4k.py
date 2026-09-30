"""Qualify the direct UHD graph contract without model loading or inference."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ltx_worker.adapter import execute, prepare_input
from ltx_worker.errors import InputError


ROOT = Path(__file__).resolve().parents[1]
MODE = "image_to_video_native_4k"
CAPS = {"MAX_GENERATION_PIXELS": "1572864", "MAX_OUTPUT_PIXELS": "2088960",
        "MAX_4K_GENERATION_PIXELS": "2088960", "MAX_4K_OUTPUT_PIXELS": "8355840",
        "MAX_NATIVE_4K_GENERATION_PIXELS": "8355840",
        "MAX_GENERATION_FRAMES": "241", "MAX_GENERATION_DURATION_S": "20"}


def request(**parameters):
    return {"mode": MODE, "prompt": "Natural subtle movement in the scene.",
            "parameters": parameters, "media": {"image": "validation-placeholder"}}


class NativeUHDTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, CAPS)
        environment.start()
        self.addCleanup(environment.stop)

    def test_diffusion_itself_is_full_resolution_with_one_sampler(self):
        spec, params, _, graph = prepare_input(request())
        self.assertEqual(spec["admission_profile"], "native_4k")
        self.assertEqual((spec["stages"], spec["output_scale"]), (1, 1))
        self.assertEqual((params["width"], params["height"], params["num_frames"]),
                         (3840, 2176, 9))
        latent = graph["video_latent"]["inputs"]
        self.assertEqual(latent, {"width": 3840, "height": 2176, "length": 9,
                                  "batch_size": 1})
        # LTX's pinned patchifier uses one token per spatial/temporal latent cell.
        self.assertEqual(((latent["length"] - 1) // 8 + 1) *
                         (latent["height"] // 32) * (latent["width"] // 32), 16320)
        samplers = [key for key, node in graph.items()
                    if node["class_type"] == "SamplerCustomAdvanced"]
        self.assertEqual(samplers, ["sample"])
        self.assertEqual(graph["sample"]["inputs"]["latent_image"], ["sample_av", 0])
        self.assertEqual(graph["sample_av"]["inputs"]["video_latent"], ["image_condition", 0])
        self.assertEqual(graph["image_condition"]["inputs"]["latent"], ["video_latent", 0])
        self.assertEqual(graph["image_condition"]["class_type"], "LTXVImgToVideoInplace")
        self.assertEqual(graph["sample_guider"]["inputs"]["cfg"], 1.0)
        self.assertEqual(len(graph["sample_sigmas"]["inputs"]["sigmas"].split(",")) - 1, 8)

    def test_output_is_a_center_crop_of_native_decode_without_upscaling(self):
        spec, _, _, graph = prepare_input(request())
        self.assertEqual(spec["output_dimensions"], {"width": 3840, "height": 2160})
        self.assertEqual(graph["crop_uhd"], {
            "class_type": "ImageCrop", "inputs": {
                "image": ["decode_video", 0], "width": 3840, "height": 2160, "x": 0, "y": 8}})
        self.assertEqual(graph["video"]["inputs"]["images"], ["crop_uhd", 0])
        self.assertEqual(graph["video"]["inputs"]["audio"], ["decode_audio", 0])
        self.assertEqual(graph["decode_video"]["inputs"]["samples"], ["sample_split", 0])
        self.assertEqual(graph["decode_audio"]["inputs"]["samples"], ["sample_split", 1])
        self.assertEqual(graph["sample_split"]["inputs"]["av_latent"], ["sample", 0])
        resizing = [key for key, node in graph.items()
                    if "Scale" in node["class_type"] or "Upscal" in node["class_type"]]
        self.assertEqual(resizing, ["image_resize"])
        self.assertEqual(graph["image_resize"]["inputs"]["image"], ["load_image", 0])
        self.assertFalse(any("Lora" in node["class_type"] or "LoRA" in node["class_type"]
                             for node in graph.values()))

    def test_models_are_the_same_as_single_stage_and_decode_is_tiled(self):
        _, _, _, graph = prepare_input(request())
        original = json.loads((ROOT / "workflows/image_to_video.json").read_text())
        inventory = json.loads((ROOT / "models/manifest.json").read_text())
        filenames = {entry["filename"].split("/")[-1] for entry in inventory["models"]}
        for key in ("model", "clip", "video_vae", "audio_vae"):
            self.assertEqual(graph[key], original[key])
            for field in ("unet_name", "clip_name", "vae_name"):
                if field in graph[key]["inputs"]:
                    self.assertIn(graph[key]["inputs"][field], filenames)
        self.assertEqual(graph["model"]["inputs"]["weight_dtype"], "default")
        self.assertEqual(graph["decode_video"]["class_type"], "VAEDecodeTiled")
        self.assertEqual({key: graph["decode_video"]["inputs"][key] for key in
                          ("tile_size", "overlap", "temporal_size", "temporal_overlap")},
                         {"tile_size": 256, "overlap": 64,
                          "temporal_size": 16, "temporal_overlap": 8})

    def test_every_node_is_reachable_and_no_dependency_cycle_exists(self):
        _, _, _, graph = prepare_input(request())
        visited, active = set(), set()

        def visit(name):
            self.assertIn(name, graph)
            self.assertNotIn(name, active, "Native UHD graph has a dependency cycle")
            if name in visited:
                return
            active.add(name)
            for value in graph[name]["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    visit(value[0])
            active.remove(name)
            visited.add(name)

        visit("save")
        self.assertEqual(visited, set(graph))

    def test_qualification_dimensions_fps_and_only_supported_frame_counts(self):
        spec, params, _, _ = prepare_input(request())
        self.assertEqual((params["num_frames"], params["fps"]), (9, 24))
        for key, value in (("width", 3840), ("height", 2176), ("fps", 24)):
            self.assertEqual(spec["constraints"][key]["enum"], [value])
        self.assertEqual(spec["constraints"]["num_frames"]["enum"], [9, 121])
        self.assertEqual(prepare_input(request(num_frames=121))[1]["num_frames"], 121)
        for override in ({"width": 1920}, {"height": 2160}, {"num_frames": 1},
                         {"num_frames": 17}, {"num_frames": 33}, {"num_frames": 129}, {"fps": 30},
                         {"cfg": 2}, {"lora_strength": 1}, {"steps": 8}):
            with self.subTest(override=override), self.assertRaises(InputError):
                prepare_input(request(**override))

    def test_requires_image_and_caps_the_aligned_render_before_cropping(self):
        payload = request()
        payload["media"] = {}
        with self.assertRaisesRegex(InputError, "Missing required media input 'image'"):
            prepare_input(payload)
        for variable, value in (("MAX_NATIVE_4K_GENERATION_PIXELS", 3840 * 2176 - 1),
                                ("MAX_4K_OUTPUT_PIXELS", 3840 * 2160),
                                ("MAX_GENERATION_FRAMES", 8),
                                ("MAX_GENERATION_DURATION_S", 0.1)):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(value)}):
                with self.assertRaisesRegex(InputError, variable):
                    prepare_input(request())

    def test_native_admission_is_separate_and_not_a_caller_override(self):
        with patch.dict(os.environ, {"MAX_GENERATION_PIXELS": "1", "MAX_OUTPUT_PIXELS": "1",
                                      "MAX_4K_GENERATION_PIXELS": "1"}):
            prepare_input(request())
        with patch.dict(os.environ, {"MAX_NATIVE_4K_GENERATION_PIXELS": "999999999"}):
            ordinary = request(width=1536, height=1536)
            ordinary["mode"] = "image_to_video"
            with self.assertRaisesRegex(InputError, "MAX_GENERATION_PIXELS"):
                prepare_input(ordinary)
        payload = request()
        payload["admission_profile"] = "native_4k"
        with self.assertRaisesRegex(InputError, "Unsupported input fields"):
            prepare_input(payload)

    def test_compile_preserves_seed_prompt_and_original_delivery_contract(self):
        prompt = "Leaves sway gently; the camera remains still."
        payload = request(seed=987654321, image_strength=0.8, num_frames=121)
        payload["prompt"] = prompt
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def ingest(source, kind, directory, role, params, **kwargs):
                self.assertEqual((kind, role), ("image", "image"))
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "condition.png"
                path.write_bytes(b"stub-media-ingestion")
                return path

            expected = {"success": True, "videos": [{"filename": "native.mp4", "url": "https://example.com/native.mp4"}],
                        "prompt_id": "original-prompt", "credit_usage": {"billed": 1}}

            def complete(job, **kwargs):
                self.assertEqual(job["id"], "native-qualification")
                graph = job["input"]["workflow"]
                self.assertEqual(graph["positive"]["inputs"]["text"], prompt)
                self.assertEqual(graph["sample_noise"]["inputs"]["noise_seed"], 987654321)
                self.assertEqual(graph["image_condition"]["inputs"]["strength"], 0.8)
                self.assertEqual(graph["video_latent"]["inputs"]["width"], 3840)
                self.assertEqual(graph["video_latent"]["inputs"]["height"], 2176)
                self.assertEqual(graph["video_latent"]["inputs"]["length"], 121)
                self.assertEqual(graph["audio_latent"]["inputs"]["frames_number"], 121)
                self.assertEqual(graph["video"]["inputs"]["fps"], 24)
                path = root / "input" / graph["load_image"]["inputs"]["image"]
                self.assertTrue(path.is_file(), "Input must remain available until result delivery")
                return expected

            with patch.dict(os.environ, {"COMFY_INPUT_DIR": str(root / "input"),
                                          "COMFY_OUTPUT_DIR": str(root / "output")}), \
                    patch("ltx_worker.adapter.ingest", side_effect=ingest):
                result = execute({"id": "native-qualification", "input": payload}, complete)
            self.assertEqual(result, expected)
            self.assertFalse(list((root / "input" / "ltx_jobs").iterdir()))


if __name__ == "__main__":
    unittest.main()
