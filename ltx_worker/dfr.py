"""Run the official DFR CLI with bounded media handling and legacy delivery.

This module stays lightweight so the local tester can import its contract without
Pillow, Torch, Runpod, or the customized handler. Model inference is a subprocess.
"""

import base64
import copy
from fractions import Fraction
import importlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .errors import InputError


DFR_MODES = frozenset({"text_to_video_dfr_4k", "image_to_video_dfr_4k"})
DEFAULT_PARAMETERS = {
    "width": 3840, "height": 2176, "num_frames": 33, "fps": 24, "seed": 42,
}
MODEL_ROOT = Path("/models/ltx25")
MODEL_FILES = {
    "transformer-path": "diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors",
    "text-encoder-path": "text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors",
    "video-vae-path": "vae/ltx-2.5-video-vae-bf16.safetensors",
    "audio-vae-path": "vae/ltx-2.5-audio-vae-bf16.safetensors",
    "spatial-upsampler-path": "latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
    "detailing-lora": "loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors",
}


def mode_specs():
    """Return independent, manifest-shaped metadata for the isolated DFR UI."""
    specs = {}
    for mode in sorted(DFR_MODES):
        defaults = dict(DEFAULT_PARAMETERS)
        defaults["prompt"] = (
            "A cinematic scene with natural motion and synchronized ambient sound."
        )
        constraints = {
            "prompt": {"type": "string", "max_length": 12000},
            "width": {"type": "integer", "enum": [3840]},
            "height": {"type": "integer", "enum": [2176]},
            "num_frames": {"type": "integer", "enum": [9, 33, 121]},
            "fps": {"type": "integer", "enum": [24]},
            "seed": {"type": "integer", "minimum": 0,
                     "maximum": 2**32 - 1},
        }
        media = {}
        if mode == "image_to_video_dfr_4k":
            defaults["image_strength"] = 0.8
            constraints["image_strength"] = {
                "type": "number", "minimum": 0, "maximum": 1,
            }
            media["image"] = {"kind": "image", "required": True}
        specs[mode] = {
            "description": "Official Python DFR pipeline to UHD with CPU offload",
            "defaults": defaults, "constraints": constraints, "media": media,
            "output_scale": 1, "output_kind": "video",
            "output_dimensions": {"width": 3840, "height": 2160},
            "admission_profile": "dfr_4k",
        }
    return specs


def validate_input(job_input):
    """Normalize the named DFR contract without decoding or downloading media."""
    if not isinstance(job_input, dict):
        raise InputError("input must be an object")
    if set(job_input) - {"mode", "prompt", "parameters", "media"}:
        raise InputError("Unsupported input fields for DFR")
    mode = job_input.get("mode")
    if not isinstance(mode, str) or mode not in DFR_MODES:
        raise InputError("Unsupported DFR mode", "UNSUPPORTED_MODE")
    prompt = job_input.get("prompt")
    if (not isinstance(prompt, str) or not prompt.strip()
            or len(prompt) > 12000 or "\x00" in prompt):
        raise InputError("DFR requires a nonempty prompt of at most 12000 characters")
    spec = mode_specs()[mode]
    supplied = job_input.get("parameters", {})
    if not isinstance(supplied, dict):
        raise InputError("parameters must be an object")
    allowed = set(spec["defaults"]) - {"prompt"}
    if set(supplied) - allowed:
        raise InputError("Unsupported DFR parameters", "UNSUPPORTED_SETTING")
    params = {key: value for key, value in spec["defaults"].items()
              if key != "prompt"}
    params.update(supplied)
    for name, value in params.items():
        rule = spec["constraints"][name]
        valid_type = (isinstance(value, int) if rule["type"] == "integer"
                      else isinstance(value, (int, float)))
        if (isinstance(value, bool) or not valid_type
                or (isinstance(value, float) and not math.isfinite(value))
                or ("enum" in rule and value not in rule["enum"])
                or value < rule.get("minimum", -math.inf)
                or value > rule.get("maximum", math.inf)):
            raise InputError(f"Unsupported value for DFR parameter '{name}'",
                             "UNSUPPORTED_SETTING")
    media = job_input.get("media", {})
    if not isinstance(media, dict) or set(media) - set(spec["media"]):
        raise InputError("Unsupported media roles for DFR")
    if "image" in spec["media"]:
        if "image" not in media:
            raise InputError("Missing required media input 'image'", "MISSING_MEDIA")
        source = media["image"]
        if isinstance(source, dict):
            if set(source) not in ({"url"}, {"base64"}):
                raise InputError("Image must contain exactly one url or base64 source")
            source = next(iter(source.values()))
        if not isinstance(source, str) or not source:
            raise InputError("Image source must be a nonempty string")
    return {"mode": mode, "prompt": prompt, "parameters": params,
            "media": copy.deepcopy(media)}


def _env_limit(name, default):
    try:
        value = int(os.environ.get(name, default))
        if value <= 0:
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise InputError(f"Worker setting {name} must be a positive integer",
                         "WORKER_CONFIGURATION") from None


def _notify(callback, job, message):
    if callback is not None:
        try:
            callback(job, message)
        except Exception:
            # Progress delivery is optional; result delivery is not.
            pass


def _terminate_process_tree(process):
    """Each subprocess owns a new group; terminate it before deleting its files."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    else:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _process_failure(phase, exit_code, diagnostics):
    if phase == "DFR pipeline":
        diagnostics.seek(0, os.SEEK_END)
        diagnostics.seek(max(0, diagnostics.tell() - 64 * 1024))
        tail = diagnostics.read(64 * 1024).decode("utf-8", errors="replace").lower()
        if "out of memory" in tail:
            return InputError("DFR exhausted GPU memory; reduce frames or use more VRAM",
                              "GPU_OUT_OF_MEMORY")
        incompatible = (
            "unsupported ptx", "ptx was compiled with an unsupported toolchain",
            "no kernel image is available", "cuda driver version is insufficient",
            "cuda error: invalid device function",
        )
        if any(message in tail for message in incompatible):
            return InputError("DFR is incompatible with the GPU driver or CUDA runtime",
                              "GPU_RUNTIME_INCOMPATIBLE")
        if "modulenotfounderror" in tail or "importerror" in tail:
            return InputError("DFR Python dependencies are unavailable or incompatible",
                              "WORKER_CONFIGURATION")
    return InputError(f"{phase} failed (exit code {exit_code})",
                      "DFR_EXECUTION_FAILED" if phase == "DFR pipeline"
                      else "OUTPUT_VALIDATION_FAILED")


def _run_command(command, timeout, phase, notify=None, capture=False):
    """Spool diagnostics privately, classify a bounded tail, never relay them."""
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        return _run_process(command, timeout, phase, diagnostics, notify, capture)


def _run_process(command, timeout, phase, diagnostics, notify, capture):
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=diagnostics, **options,
        )
    except OSError:
        raise InputError(f"Could not start {phase}; check the worker installation",
                         "WORKER_CONFIGURATION") from None
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, _ = process.communicate(timeout=min(30, remaining))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                if notify is not None:
                    notify()
        if process.returncode:
            raise _process_failure(phase, process.returncode, diagnostics)
        if capture and len(stdout or b"") > 256 * 1024:
            raise InputError("Output metadata exceeded its limit",
                             "OUTPUT_VALIDATION_FAILED")
        completed = True
        return stdout or b""
    except subprocess.TimeoutExpired:
        raise InputError(f"{phase} exceeded its execution time limit",
                         "DFR_TIMEOUT" if phase == "DFR pipeline"
                         else "OUTPUT_VALIDATION_FAILED") from None
    finally:
        if not completed:
            _terminate_process_tree(process)


def build_command(params, prompt, output, image=None):
    """Use only flags from the pinned official ltx_pipelines.dfr_pipeline CLI."""
    command = [sys.executable, "-m", "ltx_pipelines.dfr_pipeline"]
    model_root = Path(os.environ.get("DFR_MODEL_ROOT", str(MODEL_ROOT)))
    if not model_root.is_absolute():
        raise InputError("DFR_MODEL_ROOT must be absolute", "WORKER_CONFIGURATION")
    for flag, relative in MODEL_FILES.items():
        path = model_root / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise InputError(f"Required DFR model is missing: {relative}",
                             "MISSING_MODEL")
        command += ["--" + flag, str(path)]
    command += [
        "--width", str(params["width"]), "--height", str(params["height"]),
        "--num-frames", str(params["num_frames"]), "--frame-rate", "24",
        "--spatial-upscalings", "2", "--temporal-upscalings", "0",
        "--seed", str(params["seed"]), "--offload", "cpu",
        "--prompt=" + prompt, "--output-path", str(output),
    ]
    if image is not None:
        command += ["--image", str(image), "0", str(params["image_strength"])]
    return command


def _check_file(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise InputError("DFR produced no usable output file", "MISSING_OUTPUT")
    if path.stat().st_size > _env_limit("MAX_DFR_OUTPUT_BYTES", 512 * 1024**2):
        raise InputError("DFR output exceeds MAX_DFR_OUTPUT_BYTES", "RESULT_TOO_LARGE")


def _probe_video(path, height, frames):
    _check_file(path)
    command = [
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-select_streams", "v:0", "-count_frames", "-show_entries",
        "stream=codec_name,width,height,nb_frames,nb_read_frames,avg_frame_rate,duration",
        "-of", "json", str(path),
    ]
    raw = _run_command(command, _env_limit("MEDIA_PROCESS_TIMEOUT_S", 180),
                       "Video verification", capture=True)
    try:
        stream = json.loads(raw)["streams"][0]
        count = int(stream.get("nb_read_frames", stream.get("nb_frames", 0)))
        duration = float(stream["duration"])
        if (int(stream["width"]) != 3840 or int(stream["height"]) != height
                or count != frames or Fraction(stream["avg_frame_rate"]) != 24
                or not math.isfinite(duration)
                or abs(duration - frames / 24) > 1 / 48
                or (height == 2160 and stream.get("codec_name") != "h264")):
            raise ValueError()
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError,
            AttributeError, OverflowError):
        raise InputError("DFR video dimensions, frame count, FPS, or duration are invalid",
                         "OUTPUT_VALIDATION_FAILED") from None


def _crop_video(source, output):
    command = [
        "ffmpeg", "-v", "error", "-nostdin", "-y",
        "-protocol_whitelist", "file,pipe", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", "crop=3840:2160:0:8", "-c:v", "libx264",
        "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-map_metadata", "-1", "-movflags", "+faststart",
        "-threads", "2", str(output),
    ]
    _run_command(command, _env_limit("MEDIA_PROCESS_TIMEOUT_S", 180),
                 "UHD cropping")


def execute(job, *, storage=None, progress=None):
    """Run one DFR job; progress may be runpod.serverless.progress_update."""
    prompt_id = str(uuid.uuid4())
    job_id = job.get("id") if isinstance(job, dict) else None
    result = {
        "success": False, "job_id": job_id, "prompt_id": prompt_id,
        "backend": {"name": "ltx_pipelines.dfr_pipeline",
                    "prompt_id_kind": "worker_execution_uuid"},
    }
    try:
        if (not isinstance(job, dict) or not isinstance(job_id, str)
                or not job_id or len(job_id) > 256 or "input" not in job):
            raise InputError("Job must include its Runpod 'id' and 'input'")
        validated = validate_input(job["input"])
        storage = storage if storage is not None else importlib.import_module("handler")
        try:
            storage.validate_output_storage()
        except Exception:
            raise InputError("Output storage configuration is invalid; check BUCKET settings",
                             "OUTPUT_STORAGE_CONFIGURATION") from None
        result["credit_usage"] = dict(storage.build_empty_credit_usage())
        result["credit_usage"]["prompt_id"] = prompt_id
        result["comfy_credits"] = {
            "available": False, "credits_spent": None, "details": [],
        }
        timeout = _env_limit("DFR_EXECUTION_TIMEOUT_S", 3600)
        params = validated["parameters"]
        with tempfile.TemporaryDirectory(prefix="ltx-dfr-") as directory:
            root = Path(directory)
            source = root / "generated.mp4"
            output = root / "LTX25-DFR.mp4"
            image = None
            if validated["media"]:
                from .media import ingest
                _notify(progress, job, "Validating DFR image input")
                image = ingest(validated["media"]["image"], "image", root / "input",
                               "image", params)
            command = build_command(params, validated["prompt"], source, image)
            _notify(progress, job, "Running the official DFR pipeline")
            _run_command(
                command, timeout, "DFR pipeline",
                notify=lambda: _notify(progress, job, "DFR generation is running"),
            )
            _notify(progress, job, "Verifying and cropping the generated video")
            _probe_video(source, 2176, params["num_frames"])
            _crop_video(source, output)
            _probe_video(output, 2160, params["num_frames"])
            inline = not os.environ.get("BUCKET_ENDPOINT_URL")
            if inline and output.stat().st_size > storage.MAX_INLINE_OUTPUT_BYTES:
                raise InputError("Output exceeds MAX_INLINE_OUTPUT_BYTES; configure S3 delivery",
                                 "RESULT_TOO_LARGE")
            item = {"filename": output.name, "media_type": "video",
                    "format": "video/mp4", "frame_rate": 24}
            _notify(progress, job, "Delivering the verified DFR video")
            content = output.read_bytes()
            if inline:
                item.update(type="base64", data=base64.b64encode(content).decode("ascii"))
                item["warning"] = (
                    "Video returned as base64 because BUCKET_ENDPOINT_URL is not configured."
                )
            else:
                try:
                    item.update(type="s3_url", data=storage._upload_output_bytes(
                        job_id, output.name, content))
                except Exception:
                    raise InputError("Required S3 upload failed", "OUTPUT_UPLOAD_FAILED") from None
            result.update(success=True, videos=[item])
            if len(json.dumps(result).encode()) > _env_limit("MAX_RESULT_BYTES", 8 * 1024**2):
                result.pop("videos", None)
                raise InputError("Generated response exceeds MAX_RESULT_BYTES; configure S3 delivery",
                                 "RESULT_TOO_LARGE")
        return result
    except InputError as exc:
        result.update(success=False, error=str(exc), error_code=exc.code)
    except Exception:
        result.update(success=False, error="DFR worker failed while processing the job",
                      error_code="WORKER_ERROR")
    result.pop("videos", None)
    return result
