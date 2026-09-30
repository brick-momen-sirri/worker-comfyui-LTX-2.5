import unittest

from credit_estimator import estimate_credit_usage


KLING_WORKFLOW = {
    "1": {
        "inputs": {"image": "boxing.png"},
        "class_type": "LoadImage",
    },
    "2": {
        "inputs": {"filename_prefix": "video/ComfyUI", "video": ["3", 0]},
        "class_type": "SaveVideo",
    },
    "3": {
        "inputs": {
            "multi_shot": "2 storyboards",
            "multi_shot.storyboard_1_duration": 2,
            "multi_shot.storyboard_2_duration": 3,
            "generate_audio": True,
            "model": "kling-v3",
            "model.resolution": "1080p",
            "start_frame": ["1", 0],
        },
        "class_type": "KlingVideoNode",
    },
}


class TestCreditEstimator(unittest.TestCase):
    def test_estimates_kling_v3_multishot_credits(self):
        usage = estimate_credit_usage(
            KLING_WORKFLOW,
            {"outputs": {"2": {"images": [{"filename": "ComfyUI_00001_.mp4"}]}}},
            "prompt-123",
            started_node_ids={"1", "3", "2"},
            executed_node_ids={"1", "3", "2"},
            execution_success=True,
        )

        self.assertEqual(usage["source"], "estimated")
        self.assertEqual(usage["total_estimated_usd"], 0.84)
        self.assertEqual(usage["total_estimated_credits"], 177.24)
        self.assertEqual(len(usage["nodes"]), 1)
        self.assertEqual(usage["nodes"][0]["node_id"], "3")

    def test_runtime_price_overrides_fallback_estimate(self):
        history = {
            "meta": {
                "3": {
                    "api_node_price_usd": 0.70,
                }
            }
        }

        usage = estimate_credit_usage(
            KLING_WORKFLOW,
            history,
            "prompt-runtime",
            executed_node_ids={"1", "3", "2"},
            execution_success=True,
        )

        self.assertEqual(usage["source"], "runtime_price")
        self.assertEqual(usage["total_estimated_usd"], 0.70)
        self.assertEqual(usage["total_estimated_credits"], 147.7)
        self.assertEqual(usage["nodes"][0]["pricing_mode"], "runtime_price")

    def test_failed_started_node_does_not_spend_fallback_credits(self):
        usage = estimate_credit_usage(
            KLING_WORKFLOW,
            {},
            "prompt-failed",
            started_node_ids={"1", "3"},
            failed_node_ids={"3"},
            execution_success=False,
        )

        self.assertEqual(usage["total_estimated_usd"], 0.0)
        self.assertEqual(usage["total_estimated_credits"], 0.0)
        self.assertEqual(usage["nodes"][0]["pricing_mode"], "failed_before_charge")


if __name__ == "__main__":
    unittest.main()
