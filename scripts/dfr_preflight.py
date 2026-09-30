"""Check the official DFR runtime before accepting a paid generation job."""

import json
import subprocess


def main():
    import torch

    if not torch.cuda.is_available():
        raise SystemExit("DFR requires an NVIDIA CUDA GPU; CUDA is not available")
    driver = subprocess.check_output([
        "nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"
    ], text=True).splitlines()[0].strip()
    version = tuple(int(part) for part in driver.split("."))
    # Official NATTEN/Triton stack uses CUDA 13.2 and JIT kernels. CUDA minor
    # compatibility alone does not establish support for newer PTX instructions.
    if version < (595, 45, 4):
        raise SystemExit("DFR CUDA 13.2 runtime requires Linux NVIDIA driver 595.45.04 or newer; "
                         f"this host reports {driver}. Select a compatible Runpod host.")
    props = torch.cuda.get_device_properties(0)
    if props.total_memory < 44 * 1024**3:
        raise SystemExit("This DFR 4K deployment profile requires a GPU with at least 44 GiB VRAM")
    print(json.dumps({"gpu": props.name, "vram_bytes": props.total_memory,
                      "driver": driver, "torch": torch.__version__,
                      "cuda": torch.version.cuda,
                      "note": "Admission check only; 4K quality and peak VRAM require a generation test"}))


if __name__ == "__main__":
    main()
