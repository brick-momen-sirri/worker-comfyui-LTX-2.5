"""Inspect baked DFR headers and CPU tokenizer assets without loading model weights.

Run inside the completed DFR image with networking disabled. This is separate
from the full content SHA256 check in download_models.py --verify-only.
"""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import struct
import sys


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def read_header(path):
    with path.open("rb") as stream:
        size = struct.unpack("<Q", stream.read(8))[0]
        require(0 < size <= 128 * 1024**2, "Invalid safetensors header size")
        header = json.loads(stream.read(size))
    return {key: value for key, value in header.items() if key != "__metadata__"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/models-manifest.json"))
    parser.add_argument("--root", type=Path, default=Path("/models/ltx25"))
    args = parser.parse_args()
    require(args.root.is_absolute(), "Model root must be absolute")
    os.environ["DFR_MODEL_ROOT"] = str(args.root)

    from ltx_worker.dfr import DEFAULT_PARAMETERS, MODEL_FILES, build_command
    from ltx_core.loader.sft_loader import SafetensorsModelStateDictLoader
    from ltx_core.model.transformer.model_configurator import LTXV_MODEL_COMFY_RENAMING_MAP
    from ltx_core.model.audio_vae.model_configurator import (
        AUDIO_VAE_DECODER_COMFY_KEYS_FILTER, VOCODER_COMFY_KEYS_FILTER,
    )
    from ltx_core.model.video_vae.model_configurator import is_diffusion_video_vae
    from ltx_core.text_encoders.gemma.gemma_assets import (
        GemmaAssets, build_gemma_hf_config, build_gemma_hf_tokenizer,
        build_gemma_processor,
    )
    from ltx_pipelines.dfr_pipeline import add_dfr_cli_args
    from ltx_pipelines.iclora_utils import read_lora_reference_downscale_factor
    from ltx_pipelines.utils.args import (
        default_2_stage_distilled_arg_parser, resolve_cli_params,
    )

    entries = json.loads(args.manifest.read_text())["models"]
    require(len(entries) == 6, "Expected the six-file DFR bundle")
    require({entry["destination"] for entry in entries} == set(MODEL_FILES.values()),
            "Manifest and worker component paths differ")
    headers, metadata, report = {}, {}, []
    loader = SafetensorsModelStateDictLoader()
    for entry in entries:
        relative = entry["destination"]
        path = args.root / relative
        require(path.is_file() and not path.is_symlink(), "Missing regular model file")
        require(path.stat().st_size == entry["size"], "Model size differs from manifest")
        headers[relative] = read_header(path)
        require(bool(headers[relative]), "Model contains no tensor entries")
        metadata[relative] = loader.metadata(str(path))
        report.append({
            "path": relative, "bytes": entry["size"],
            "tensor_count": len(headers[relative]),
            "dtypes": dict(Counter(item["dtype"] for item in headers[relative].values())),
        })

    transformer = MODEL_FILES["transformer-path"]
    text_encoder = MODEL_FILES["text-encoder-path"]
    video_vae = MODEL_FILES["video-vae-path"]
    audio_vae = MODEL_FILES["audio-vae-path"]
    lora = MODEL_FILES["detailing-lora"]
    transformer_config = metadata[transformer]["config"]["transformer"]
    require(transformer_config.get("use_keyframes_abs_pos_embedding") is True,
            "Transformer does not declare official generated-keyframe support")
    for relative in (transformer, text_encoder):
        require(any(item["dtype"] == "BF16" for item in headers[relative].values()),
                "Expected BF16 transformer and text encoder")
    require(is_diffusion_video_vae(str(args.root / video_vae)),
            "DFR bundle must use the diffusion video VAE")
    for name, sd_ops in (("audio decoder", AUDIO_VAE_DECODER_COMFY_KEYS_FILTER),
                         ("audio vocoder", VOCODER_COMFY_KEYS_FILTER)):
        require(any(sd_ops.apply_to_key(key) for key in headers[audio_vae]),
                "No keys accepted by official " + name + " loader")

    # These builders read only the embedded tokenizer and JSON sidecars.
    # They never instantiate Gemma's multi-billion-parameter language model.
    assets = GemmaAssets.load(args.root / text_encoder)
    config = build_gemma_hf_config(assets)
    tokenizer = build_gemma_hf_tokenizer(assets)
    processor = build_gemma_processor(assets, tokenizer)
    require(bool(tokenizer("A calm scene.")["input_ids"]), "Tokenizer returned no tokens")

    command = build_command(DEFAULT_PARAMETERS, "CPU metadata verification.",
                            Path("/tmp/dfr-metadata-unused.mp4"))
    saved_argv = sys.argv
    try:
        sys.argv = [command[2], *command[3:]]
        pipeline_params = resolve_cli_params(distilled=True)
        cli = add_dfr_cli_args(default_2_stage_distilled_arg_parser(
            params=pipeline_params, supports_auto_duration=True))
        parsed = cli.parse_args(command[3:])
    finally:
        sys.argv = saved_argv
    require(parsed.model_paths.mode == "split", "Official CLI did not select split models")
    require(parsed.spatial_upscalings == 2 and parsed.temporal_upscalings == 0,
            "Official CLI stages differ from DFR contract")
    require(read_lora_reference_downscale_factor(str(args.root / lora)) == 2,
            "Pixel LoRA does not declare a 2x reference scale")

    base = {mapped: item["shape"] for key, item in headers[transformer].items()
            if (mapped := LTXV_MODEL_COMFY_RENAMING_MAP.apply_to_key(key)) is not None}
    lora_ops = parsed.detailing_lora[0].sd_ops
    adapter = {mapped: item["shape"] for key, item in headers[lora].items()
               if (mapped := lora_ops.apply_to_key(key)) is not None}
    pairs = 0
    for key, a in adapter.items():
        if not key.endswith(".lora_A.weight"):
            continue
        prefix = key.removesuffix(".lora_A.weight")
        b = adapter.get(prefix + ".lora_B.weight")
        weight = base.get(prefix + ".weight")
        require(b is not None and weight is not None, "Pixel LoRA target/pair is missing")
        require(len(a) == len(b) == len(weight) == 2
                and a[0] == b[1] and [b[0], a[1]] == weight,
                "Pixel LoRA tensor shapes do not match the transformer")
        pairs += 1
    require(pairs > 0, "No pixel LoRA pairs match the official transformer mapping")
    print(json.dumps({
        "models": report, "official_split_cli": "passed",
        "generated_keyframes": True, "diffusion_video_vae": True,
        "pixel_lora_matching_pairs": pairs,
        "gemma_config_type": type(config).__name__,
        "gemma_processor_type": type(processor).__name__,
        "gemma_tokenizer": "passed",
        "scope": "CPU headers and embedded assets only; full weight loading, GPU generation, S3 and visual quality were not tested",
    }))


if __name__ == "__main__":
    main()
