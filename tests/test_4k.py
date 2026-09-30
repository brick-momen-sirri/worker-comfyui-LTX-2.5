"""Validate UHD topology and admission without loading models or using a GPU."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ltx_worker.adapter import execute, load_manifest, prepare_input
from ltx_worker.errors import InputError


ROOT = Path(__file__).resolve().parents[1]
MODES = ("image_to_video_4k", "video_upscale_4k")
CAPS = {"MAX_GENERATION_PIXELS": "1572864", "MAX_OUTPUT_PIXELS": "2088960",
        "MAX_4K_GENERATION_PIXELS": "2088960", "MAX_4K_OUTPUT_PIXELS": "8355840",
        "MAX_GENERATION_FRAMES": "241", "MAX_GENERATION_DURATION_S": "20"}


def request(mode, **parameters):
    role = "image" if mode.startswith("image_") else "video"
    return {"mode": mode, "prompt": "A slow camera move with natural detail.",
            "parameters": parameters, "media": {role: "validation-placeholder"}}


class UHDWorkflowTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, CAPS)
        environment.start()
        self.addCleanup(environment.stop)

    def test_exact_dimensions_short_defaults_and_source_contract(self):
        for mode, source, scale in ((MODES[0], (960, 544), 4),
                                    (MODES[1], (1920, 1088), 2)):
            with self.subTest(mode=mode):
                spec, params, _, graph = prepare_input(request(mode))
                self.assertEqual(spec["admission_profile"], "4k")
                self.assertEqual(spec["output_dimensions"], {"width": 3840, "height": 2160})
                self.assertEqual(spec["output_scale"], scale)
                self.assertEqual((params["width"], params["height"]), source)
                self.assertEqual((params["num_frames"], params["fps"]), (9, 24))
                self.assertEqual((source[0] * scale, source[1] * scale), (3840, 2176))
                crop = graph["crop_uhd"]
                self.assertEqual(crop["class_type"], "ImageCrop")
                self.assertEqual({key: crop["inputs"][key] for key in ("width", "height", "x", "y")},
                                 {"width": 3840, "height": 2160, "x": 0, "y": 8})
                self.assertEqual(graph["video"]["inputs"]["images"], ["crop_uhd", 0])
                self.assertEqual(graph[crop["inputs"]["image"][0]]["class_type"], "VAEDecodeTiled")

    def test_every_reference_is_acyclic_and_reachable_from_the_result(self):
        for mode in MODES:
            _, _, _, graph = prepare_input(request(mode))
            visited, active = set(), set()

            def visit(name):
                self.assertIn(name, graph)
                self.assertNotIn(name, active, "Dependency cycle in " + mode)
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

    def test_i2v_pixel_pass_uses_completed_two_stage_video_and_frozen_audio(self):
        _, _, _, graph = prepare_input(request(MODES[0]))
        self.assertEqual(graph["control"]["inputs"]["image"], ["decode_video", 0])
        self.assertEqual(graph["decode_video"]["inputs"]["samples"], ["refine_split", 0])
        self.assertEqual(graph["decode_audio"]["inputs"]["samples"], ["refine_split", 1])
        self.assertEqual(graph["trim_audio"]["inputs"]["audio"], ["decode_audio", 0])
        self.assertEqual(graph["pixel_av"]["inputs"]["audio_latent"], ["audio_reference", 2])
        self.assertEqual(graph["pixel_decode_video"]["inputs"]["samples"], ["pixel_crop", 2])
        self.assertEqual(graph["video"]["inputs"]["audio"], ["trim_audio", 0])
        self.assertEqual(graph["pixel_video_latent"]["inputs"]["width"], 3840)
        self.assertEqual(graph["pixel_video_latent"]["inputs"]["height"], 2176)

    def test_models_stay_bundled_and_tiled_decode_uses_small_tiles(self):
        inventory = json.loads((ROOT / "models/manifest.json").read_text())
        filenames = {entry["filename"].split("/")[-1] for entry in inventory["models"]}
        for mode in MODES:
            _, _, _, graph = prepare_input(request(mode))
            for node in graph.values():
                for field in ("unet_name", "clip_name", "vae_name", "lora_name", "model_name"):
                    if field in node["inputs"]:
                        self.assertIn(node["inputs"][field], filenames)
                if node["class_type"] == "VAEDecodeTiled":
                    self.assertEqual({key: node["inputs"][key] for key in
                                      ("tile_size", "overlap", "temporal_size", "temporal_overlap")},
                                     {"tile_size": 256, "overlap": 64,
                                      "temporal_size": 16, "temporal_overlap": 8})
            self.assertEqual(graph["control"]["inputs"]["latent_downscale_factor"], 2.0)
            self.assertTrue(graph["control"]["inputs"]["use_tiled_encode"])
            self.assertEqual(graph["iclora"]["inputs"]["strength_model"], 1.0)
            self.assertEqual(sum(node["class_type"] == "UNETLoader" for node in graph.values()), 1)

    def test_fixed_dimensions_and_frame_limit_are_enforced(self):
        for mode in MODES:
            self.assertEqual(prepare_input(request(mode, num_frames=33))[1]["num_frames"], 33)
            for override in ({"num_frames": 41}, {"num_frames": 10}, {"width": 512},
                             {"height": 512}, {"lora_strength": 0.5}, {"control_strength": 0.5}):
                with self.subTest(mode=mode, override=override), self.assertRaises(InputError):
                    prepare_input(request(mode, **override))

    def test_all_defaults_and_compiled_bindings_match_including_duration(self):
        for mode in MODES:
            spec, params, _, graph = prepare_input(request(mode))
            values = dict(spec["defaults"], duration=params["num_frames"] / params["fps"])
            for parameter, bindings in spec["bindings"].items():
                unique = {(binding["node_id"], binding["input"]) for binding in bindings}
                self.assertEqual(len(unique), len(bindings), "Duplicate " + parameter + " binding")
                for binding in bindings:
                    self.assertEqual(graph[binding["node_id"]]["inputs"][binding["input"]],
                                     values[parameter] * binding.get("multiplier", 1))
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)

                def ingest(source, kind, directory, role, params, **kwargs):
                    directory.mkdir(parents=True, exist_ok=True)
                    path = directory / (role + (".png" if kind == "image" else ".mp4"))
                    path.write_bytes(b"mock-media")
                    return path

                def complete(job, **kwargs):
                    self.assertEqual(job["id"], "original-job")
                    compiled = job["input"]["workflow"]
                    for binding in spec["bindings"]["num_frames"]:
                        self.assertEqual(compiled[binding["node_id"]]["inputs"][binding["input"]], 33)
                    for binding in spec["bindings"]["seed"]:
                        self.assertEqual(compiled[binding["node_id"]]["inputs"][binding["input"]], 123)
                    self.assertEqual(compiled["trim_audio"]["inputs"]["duration"], 33 / 24)
                    return {"success": True, "videos": [{"filename": "result.mp4"}], "prompt_id": "same-contract"}

                with patch.dict(os.environ, {"COMFY_INPUT_DIR": str(root / "input"),
                                              "COMFY_OUTPUT_DIR": str(root / "output")}), \
                        patch("ltx_worker.adapter.ingest", side_effect=ingest):
                    result = execute({"id": "original-job", "input": request(mode, num_frames=33, seed=123)}, complete)
                self.assertTrue(result.get("success"), result)
                self.assertEqual(result["prompt_id"], "same-contract")

    def test_4k_caps_account_for_aligned_render_and_global_frame_policies(self):
        with patch.dict(os.environ, {"MAX_4K_OUTPUT_PIXELS": str(3840 * 2160)}):
            for mode in MODES:
                with self.assertRaisesRegex(InputError, "MAX_4K_OUTPUT_PIXELS"):
                    prepare_input(request(mode))
        with patch.dict(os.environ, {"MAX_4K_GENERATION_PIXELS": str(1920 * 1088 - 1)}):
            with self.assertRaisesRegex(InputError, "MAX_4K_GENERATION_PIXELS"):
                prepare_input(request(MODES[1]))
        for setting, value in (("MAX_GENERATION_FRAMES", "8"),
                               ("MAX_GENERATION_DURATION_S", "0.1")):
            with patch.dict(os.environ, {setting: value}):
                for mode in MODES:
                    with self.assertRaises(InputError):
                        prepare_input(request(mode))

    def test_legacy_caps_remain_separate_and_callers_cannot_set_profile(self):
        with patch.dict(os.environ, {"MAX_GENERATION_PIXELS": "1", "MAX_OUTPUT_PIXELS": "1"}):
            for mode in MODES:
                prepare_input(request(mode))
            with self.assertRaisesRegex(InputError, "MAX_GENERATION_PIXELS"):
                prepare_input(request("image_to_video"))
        with patch.dict(os.environ, {"MAX_4K_GENERATION_PIXELS": "999999999", "MAX_4K_OUTPUT_PIXELS": "999999999"}):
            with self.assertRaisesRegex(InputError, "MAX_GENERATION_PIXELS"):
                prepare_input(request("image_to_video", width=1536, height=1536))
            with self.assertRaisesRegex(InputError, "MAX_OUTPUT_PIXELS"):
                prepare_input(request("video_upscale_x2", width=1024, height=544))
        for mode in MODES:
            payload = request(mode)
            payload["admission_profile"] = "4k"
            with self.assertRaises(InputError):
                prepare_input(payload)
        profiles = {mode for mode, spec in load_manifest()["modes"].items()
                    if spec.get("admission_profile") == "4k"}
        self.assertEqual(profiles, set(MODES))


if __name__ == "__main__":
    unittest.main()
