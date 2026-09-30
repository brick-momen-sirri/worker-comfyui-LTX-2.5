import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location("gpu_preflight", Path(__file__).resolve().parents[1] / "scripts/gpu_preflight.py")
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class GPUPolicyTests(unittest.TestCase):
    def test_profiles_allow_nominal_24_and_48_gb_cards(self):
        self.assertLess(POLICY.minimum_vram("int8"), 24564 / 1024)
        self.assertEqual(POLICY.minimum_vram("bf16"), 47)
        self.assertEqual(POLICY.minimum_vram("cq-v2"), 47)
        self.assertEqual(POLICY.minimum_vram("int8", ""), 23)

    def test_operator_override_and_invalid_configuration(self):
        self.assertEqual(POLICY.minimum_vram("int8", "16"), 16)
        for value in ("nan", "inf", "-1", "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                POLICY.minimum_vram("int8", value)
        with self.assertRaises(ValueError):
            POLICY.minimum_vram("unknown")
