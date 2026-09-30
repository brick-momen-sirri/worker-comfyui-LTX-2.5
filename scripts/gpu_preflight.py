"""Check CUDA and the selected profile's admission policy, not peak workload fit."""
import json
import math
import os
from pathlib import Path


def minimum_vram(profile, override=None):
    defaults = {"int8": 23.0, "bf16": 47.0, "cq-v2": 47.0}
    if profile not in defaults:
        raise ValueError("Unsupported model precision in workflow manifest")
    minimum = defaults[profile] if override in (None, "") else float(override)
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError("LTX_MIN_VRAM_GB must be a nonnegative finite GiB value")
    return minimum


def main():
    import torch

    profile = json.loads(Path("/workflows/manifest.json").read_text())["precision"]
    minimum = minimum_vram(profile, os.getenv("LTX_MIN_VRAM_GB"))
    torch.cuda.init()
    value = (torch.zeros(8, device="cuda") + 1).sum().item()
    torch.cuda.synchronize()
    if value != 8:
        raise RuntimeError("CUDA kernel preflight returned an unexpected result")
    if not torch.cuda.is_bf16_supported(including_emulation=False):
        raise RuntimeError("This worker's GPU policy requires native BF16 support for its VAE components")
    properties = torch.cuda.get_device_properties(0)
    vram_gib = properties.total_memory / 1024**3
    free_gib = torch.cuda.mem_get_info()[0] / 1024**3
    if vram_gib < minimum:
        raise RuntimeError(
            f"The {profile.upper()} worker admission policy requires {minimum:g} GiB total VRAM; "
            f"found {vram_gib:.1f} GiB. This is an operator policy, not an LTX model minimum. "
            "LTX_MIN_VRAM_GB can override admission but cannot guarantee memory fit."
        )
    print(f"CUDA ready: {properties.name}, total={vram_gib:.1f} GiB, free={free_gib:.1f} GiB, "
          f"profile={profile}, torch={torch.__version__}, CUDA={torch.version.cuda}")
    print("Admission passed; generation VRAM still depends on dimensions, frame count, "
          "conditioning, other GPU processes, and offloading.")


if __name__ == "__main__":
    main()
