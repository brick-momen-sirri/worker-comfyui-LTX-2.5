"""CPU build checks; does not claim that GPU inference or model loading passed."""

import importlib.metadata as metadata
import json
import subprocess
import sys


def main():
    import torch
    import torchaudio
    import torchvision
    import natten
    from ltx_pipelines.dfr_pipeline import DFRPipeline, add_dfr_cli_args
    from ltx_core.model.video_vae import VideoDecoder
    import dfr_worker

    # Upstream deliberately overrides this one strict dependency. Fail every
    # other pip-check error instead of hiding unrelated resolver regressions.
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    expected = "torch 2.13.0+cu132 has requirement nvidia-cudnn-cu13==9.20.0.48"
    unexpected = [line for line in check.stdout.splitlines()
                  if line != "No broken requirements found." and not line.startswith(expected)]
    if unexpected or check.stderr or (check.returncode and not check.stdout.strip()):
        raise RuntimeError("Unexpected dependency conflict: " + "\n".join(unexpected))
    assert metadata.version("nvidia-cudnn-cu13") == "9.24.0.43"
    assert callable(DFRPipeline) and callable(add_dfr_cli_args) and callable(dfr_worker.handler)
    # The official test-index audio wheel's metadata and internal version string
    # differ. Check real CPU operations too; CUDA operation remains a GPU check.
    resampled = torchaudio.functional.resample(torch.zeros(1, 4800), 48000, 24000)
    assert resampled.shape == (1, 2400)
    assert torch.isfinite(resampled).all()
    torchvision.ops.nms(torch.tensor([[0., 0., 1., 1.]]), torch.ones(1), 0.5)
    print(json.dumps({"torch": torch.__version__, "cuda": torch.version.cuda,
                      "torchaudio": torchaudio.__version__, "torchvision": torchvision.__version__,
                      "torchaudio_distribution": metadata.version("torchaudio"),
                      "audio_cpu_resample": "passed", "vision_cpu_nms": "passed",
                      "natten": metadata.version("natten"),
                      "transformers": metadata.version("transformers"),
                      "upstream_cudnn_override": "9.24.0.43",
                      "dfr_import": "passed", "gpu_generation": "not tested"}))


if __name__ == "__main__":
    main()
