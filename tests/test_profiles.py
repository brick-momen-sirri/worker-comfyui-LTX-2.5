"""Prove standard and CQ builds select coherent graphs and pinned auxiliaries."""

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.configure_profile import configure_profile
from scripts.download_models import load_manifest
from scripts import validate_workflows

ROOT = Path(__file__).resolve().parents[1]


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "models" / "profiles", self.root / "models" / "profiles")
        shutil.copytree(ROOT / "workflows", self.root / "workflows")

    def read(self, path):
        return json.loads((self.root / path).read_text(encoding="utf-8"))

    def test_profiles_have_exact_pinned_model_totals(self):
        for profile, count, total in (("int8", 11, 44542597655),
                                      ("bf16", 11, 75947642823),
                                      ("cq-v2", 6, 48934613516)):
            with self.subTest(profile=profile):
                entries = load_manifest(self.root / "models" / "profiles" / (profile + ".json"))
                self.assertEqual(len(entries), count)
                self.assertEqual(sum(entry["size"] for entry in entries), total)

    def test_profiles_change_only_transformer_and_encoder_weights(self):
        int8 = self.read("models/profiles/int8.json")
        bf16 = self.read("models/profiles/bf16.json")
        changed = []
        for left, right in zip(int8["models"], bf16["models"]):
            if left != right:
                changed.append(left["destination"].split("/")[0])
                self.assertEqual(left["repo"], right["repo"])
                self.assertEqual(left["revision"], right["revision"])
        self.assertEqual(changed, ["diffusion_models", "text_encoders"])

    def test_exact_official_int8_filenames_and_hashes(self):
        entries = {item["destination"]: item for item in self.read("models/profiles/int8.json")["models"]}
        expected = {
            "diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors": (
                21504034224, "c4279eeff115cbeaca494bd2183e7d768c38fe85a184dc6afbb7159157c44334"),
            "text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors": (
                15372969374, "6ce688a0aa98a5fa36a9f1e6c3f42152a498cc2b53ee8c15674c64244f91487f"),
        }
        for filename, (size, sha256) in expected.items():
            self.assertEqual(entries[filename]["filename"], filename)
            self.assertEqual(entries[filename]["revision"], "5e6e71018ee1756ed329b697a7b4aedc934dfce9")
            self.assertEqual(entries[filename]["size"], size)
            self.assertEqual(entries[filename]["sha256"], sha256)

    def test_both_profiles_validate_all_graphs_and_active_inventories(self):
        for profile in ("int8", "bf16"):
            with self.subTest(profile=profile):
                result = configure_profile(self.root, profile)
                self.assertEqual(result["workflows"], 21)
                inventory = self.read("models/profiles/" + profile + ".json")
                self.assertEqual(self.read("models/manifest.json"), inventory)
                self.assertEqual(self.read("models-manifest.json"), inventory)
                self.assertEqual(self.read("workflows/manifest.json")["precision"], profile)
                with patch.object(validate_workflows, "ROOT", self.root):
                    report = validate_workflows.validate(self.root / "workflows")
                self.assertTrue(report["success"], report["errors"])
                self.assertEqual(report["mode_count"], 21)
                self.assertEqual(report["unique_classes"], 55)

    def test_cq_profile_is_isolated_and_validates_its_single_graph(self):
        result = configure_profile(self.root, "cq-v2")
        self.assertEqual(result, {
            "profile": "cq-v2", "workflows": 1, "models": 6,
            "model_bytes": 48934613516,
        })
        inventory = self.read("models/profiles/cq-v2.json")
        self.assertEqual(self.read("models/manifest.json"), inventory)
        self.assertEqual(self.read("models-manifest.json"), inventory)
        manifest = self.read("workflows/manifest.json")
        self.assertEqual(manifest["precision"], "cq-v2")
        self.assertEqual(set(manifest["modes"]), {"video_enhance_cq_v2"})
        with patch.object(validate_workflows, "ROOT", self.root):
            report = validate_workflows.validate(self.root / "workflows")
        self.assertTrue(report["success"], report["errors"])
        self.assertEqual(report["mode_count"], 1)

    def test_switching_from_cq_back_to_standard_restores_standard_contract(self):
        configure_profile(self.root, "cq-v2")
        self.assertEqual(set(self.read("workflows/manifest.json")["modes"]), {"video_enhance_cq_v2"})
        configure_profile(self.root, "int8")
        self.assertEqual(self.read("workflows/manifest.json"), self.read("workflows/manifest.standard.json"))
        self.assertEqual(len(self.read("workflows/manifest.json")["modes"]), 21)

    def test_profile_switch_preserves_graph_settings_and_round_trips(self):
        configure_profile(self.root, "int8")
        manifest = self.read("workflows/manifest.json")
        original = {mode: self.read("workflows/" + spec["file"]) for mode, spec in manifest["modes"].items()}
        configure_profile(self.root, "bf16")
        for mode, spec in manifest["modes"].items():
            graph = self.read("workflows/" + spec["file"])
            expected = copy.deepcopy(original[mode])
            expected["model"]["inputs"]["unet_name"] = "ltx-2.5-22b-distilled-transformer-bf16.safetensors"
            expected["clip"]["inputs"]["clip_name"] = "gemma4-12b-with-proj-ltx-2.5-bf16.safetensors"
            self.assertEqual(graph, expected)
        configure_profile(self.root, "int8")
        self.assertEqual(self.read("workflows/manifest.json"), manifest)
        for mode, spec in manifest["modes"].items():
            self.assertEqual(self.read("workflows/" + spec["file"]), original[mode])

    def test_other_nodes_and_unknown_loader_filenames_are_not_rewritten(self):
        filename = "workflows/text_to_video.json"
        graph = self.read(filename)
        model_name = graph["model"]["inputs"]["unet_name"]
        graph["positive"]["inputs"]["text"] = model_name
        graph["custom_loader"] = {"class_type": "UNETLoader", "inputs": {"unet_name": "custom.safetensors"}}
        (self.root / filename).write_text(json.dumps(graph), encoding="utf-8")
        configure_profile(self.root, "bf16")
        updated = self.read(filename)
        self.assertEqual(updated["positive"], graph["positive"])
        self.assertEqual(updated["custom_loader"], graph["custom_loader"])

    def test_invalid_profile_or_broken_graph_leaves_configuration_unchanged(self):
        configure_profile(self.root, "int8")
        before = (self.root / "models/manifest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "Unsupported model profile"):
            configure_profile(self.root, "unknown")
        manifest = self.read("workflows/manifest.standard.json")
        manifest["modes"]["video_upscale_x2"]["file"] = "../outside.json"
        (self.root / "workflows/manifest.standard.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsafe workflow path"):
            configure_profile(self.root, "bf16")
        self.assertEqual((self.root / "models/manifest.json").read_bytes(), before)
        self.assertIn("int8-convrot", self.read("workflows/text_to_video.json")["model"]["inputs"]["unet_name"])


if __name__ == "__main__":
    unittest.main()
