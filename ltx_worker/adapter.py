"""Compile the versioned input contract to official ComfyUI API graphs."""

import copy
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import threading

from .errors import InputError

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
JOB_LOCK = threading.Lock()


def ingest(*args, **kwargs):
    # Contract validation and workflow compilation do not decode media. Keep the
    # Pillow dependency local to ingestion so the lightweight tester can compile.
    from .media import ingest as ingest_media
    return ingest_media(*args, **kwargs)


def load_manifest():
    try:
        return json.loads((WORKFLOWS / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InputError("Worker workflow manifest is missing or invalid", "WORKER_CONFIGURATION") from None


def _set(workflow, binding, value):
    try:
        workflow[str(binding["node_id"])]["inputs"][binding["input"]] = value
    except (KeyError, TypeError):
        raise InputError("Worker workflow binding is invalid", "WORKER_CONFIGURATION") from None


def _validate_value(name, value, rule):
    kind = rule.get("type")
    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        raise InputError(f"Parameter '{name}' must be an integer", "UNSUPPORTED_SETTING")
    if kind == "number" and (isinstance(value, bool) or not isinstance(value, (float, int)) or (isinstance(value, float) and not math.isfinite(value))):
        raise InputError(f"Parameter '{name}' must be a finite number", "UNSUPPORTED_SETTING")
    if kind == "string" and not isinstance(value, str):
        raise InputError(f"Parameter '{name}' must be a string", "UNSUPPORTED_SETTING")
    if kind == "boolean" and not isinstance(value, bool):
        raise InputError(f"Parameter '{name}' must be a boolean", "UNSUPPORTED_SETTING")
    if "enum" in rule and value not in rule["enum"]:
        raise InputError(f"Parameter '{name}' must be one of {rule['enum']}", "UNSUPPORTED_SETTING")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            raise InputError(f"Parameter '{name}' must be finite", "UNSUPPORTED_SETTING")
        if value < rule.get("minimum", -math.inf) or value > rule.get("maximum", math.inf):
            raise InputError(f"Parameter '{name}' is outside the supported range", "UNSUPPORTED_SETTING")
        multiple = rule.get("multiple_of", rule.get("multipleOf"))
        if multiple and (value - rule.get("offset", 0)) % multiple:
            raise InputError(f"Parameter '{name}' must be {rule.get('offset', 0)} plus a multiple of {multiple}", "UNSUPPORTED_SETTING")
    if isinstance(value, str) and len(value) > rule.get("max_length", rule.get("maxLength", 16000)):
        raise InputError(f"Parameter '{name}' is too long", "UNSUPPORTED_SETTING")


def _validate_tracks(params):
    if "tracks_json" not in params:
        return
    try:
        tracks = json.loads(params["tracks_json"])
        if not isinstance(tracks, list) or not 1 <= len(tracks) <= 32:
            raise ValueError()
        for track in tracks:
            if not isinstance(track, list) or len(track) != params["num_frames"]:
                raise ValueError()
            for point in track:
                if not isinstance(point, dict) or set(point) != {"x", "y"}:
                    raise ValueError()
                for coordinate, bound in (("x", params["width"]), ("y", params["height"])):
                    value = point[coordinate]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < bound or (isinstance(value, float) and not math.isfinite(value)):
                        raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise InputError("tracks_json must contain 1–32 tracks, each with num_frames {x,y} points within the source canvas", "UNSUPPORTED_SETTING") from None


def prepare_input(job_input, *, manifest=None):
    if not isinstance(job_input, dict):
        raise InputError("input must be an object")
    allowed = {"mode", "prompt", "negative_prompt", "parameters", "media"}
    unknown = set(job_input) - allowed
    if unknown:
        raise InputError("Unsupported input fields for a named mode: " + ", ".join(sorted(unknown)))
    manifest = load_manifest() if manifest is None else manifest
    modes = manifest.get("modes", manifest)
    mode = job_input.get("mode")
    if not isinstance(mode, str) or mode not in modes:
        raise InputError("Unknown mode; supported modes: " + ", ".join(sorted(modes)), "UNSUPPORTED_MODE")
    spec = modes[mode]
    overrides = job_input.get("parameters", {})
    if not isinstance(overrides, dict):
        raise InputError("parameters must be an object")
    params = copy.deepcopy(spec["defaults"])
    if set(overrides) - set(params):
        raise InputError("Unsupported parameters: " + ", ".join(sorted(set(overrides) - set(params))), "UNSUPPORTED_SETTING")
    params.update(overrides)
    prompt_required = spec.get("prompt_required", True)
    if prompt_required and "prompt" not in job_input and "prompt" not in overrides:
        raise InputError("A nonempty prompt is required")
    if "tracks_json" in params and "tracks_json" not in overrides:
        raise InputError("motion_track requires parameters.tracks_json", "MISSING_MEDIA")
    for key in ("prompt", "negative_prompt"):
        if key in job_input:
            if key in overrides:
                raise InputError(f"Supply '{key}' at top level or in parameters, not both")
            if key not in params:
                raise InputError(f"'{key}' is not configurable for this mode", "UNSUPPORTED_SETTING")
            params[key] = job_input[key]
    if (not isinstance(params.get("prompt"), str)
            or (prompt_required and not params["prompt"].strip())):
        raise InputError("A nonempty prompt is required")
    for key, value in params.items():
        rule = spec.get("constraints", {}).get(key)
        if rule is None:
            default = spec["defaults"][key]
            rule = {"type": "boolean" if isinstance(default, bool) else "integer" if isinstance(default, int) else "number" if isinstance(default, float) else "string"}
        _validate_value(key, value, rule)
    _validate_tracks(params)
    # Admission caps protect the worker. Model maxima are not VRAM guarantees.
    width, height = params.get("width", 64), params.get("height", 64)
    frames = params.get("num_frames", 121)
    if frames / params.get("fps", 24) > float(os.environ.get("MAX_GENERATION_DURATION_S", 20)):
        raise InputError("Requested duration exceeds MAX_GENERATION_DURATION_S", "UNSUPPORTED_SETTING")
    # Only trusted manifest entries select the separate 4K admission policy.
    # Their output_scale describes the aligned render before the final crop.
    native_4k = spec.get("admission_profile") == "native_4k"
    cq = spec.get("admission_profile") == "cq_v2"
    high_resolution = native_4k or spec.get("admission_profile") == "4k"
    generation_cap = ("MAX_NATIVE_4K_GENERATION_PIXELS" if native_4k else
                      "MAX_4K_GENERATION_PIXELS" if high_resolution else
                      "MAX_CQ_GENERATION_PIXELS" if cq else "MAX_GENERATION_PIXELS")
    generation_default = 3840 * 2176 if native_4k else 1920 * 1088 if high_resolution or cq else 1536 * 1024
    if width * height > int(os.environ.get(generation_cap, generation_default)):
        raise InputError(f"Requested dimensions exceed {generation_cap}", "UNSUPPORTED_SETTING")
    cq_high_res_frames = int(os.environ.get("MAX_CQ_HIGH_RES_FRAMES", 25))
    if cq and width * height > 1920 * 1088 and frames > cq_high_res_frames:
        raise InputError(f"CQ canvases above Full HD are limited to {cq_high_res_frames} frames by MAX_CQ_HIGH_RES_FRAMES", "UNSUPPORTED_SETTING")
    if frames > int(os.environ.get("MAX_GENERATION_FRAMES", 241)):
        raise InputError("num_frames exceeds MAX_GENERATION_FRAMES", "UNSUPPORTED_SETTING")
    factor = spec.get("output_scale", 1)
    final_width = (width + params.get("pad_left", 0) + params.get("pad_right", 0)) * factor
    final_height = (height + params.get("pad_top", 0) + params.get("pad_bottom", 0)) * factor
    output_cap = "MAX_4K_OUTPUT_PIXELS" if high_resolution else "MAX_CQ_OUTPUT_PIXELS" if cq else "MAX_OUTPUT_PIXELS"
    output_default = (3840 * 2176 if high_resolution else
                      int(os.environ.get("MAX_OUTPUT_PIXELS", 1920 * 1088)) if cq else 1920 * 1088)
    if final_width * final_height > int(os.environ.get(output_cap, output_default)):
        raise InputError(f"Expanded/upscaled output exceeds {output_cap}", "UNSUPPORTED_SETTING")
    media = job_input.get("media", {})
    if not isinstance(media, dict):
        raise InputError("media must be an object")
    bindings = spec.get("media", {})
    if set(media) - set(bindings):
        raise InputError("Unsupported media roles: " + ", ".join(sorted(set(media) - set(bindings))))
    for role, binding in bindings.items():
        if binding.get("required", True) and role not in media:
            raise InputError(f"Missing required media input '{role}'", "MISSING_MEDIA")
    try:
        workflow_path = (WORKFLOWS / spec.get("file", spec.get("workflow", ""))).resolve()
        workflow_path.relative_to(WORKFLOWS.resolve())
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise InputError("Worker workflow file is missing or invalid", "WORKER_CONFIGURATION") from None
    return spec, params, media, workflow


def _remove_job_directory(root, directory):
    """Only remove our immediate UUID child; never accept user-supplied paths."""
    root, directory = Path(root).resolve(), Path(directory).resolve()
    if directory.parent != root or not re.fullmatch(r"[0-9a-f]{32}", directory.name):
        raise RuntimeError("Refusing cleanup outside this worker's job directory")
    if directory.exists():
        shutil.rmtree(directory)


def execute(job, legacy_handler):
    """The Runpod envelope/id remains intact; completion belongs to the legacy handler."""
    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
        return {"error": "Runpod job envelope must contain a nonempty string id", "error_code": "INVALID_INPUT"}
    job_input = job.get("input")
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except ValueError:
            return {"error": "Invalid JSON format in input", "error_code": "INVALID_INPUT"}
    # Legacy arbitrary workflows retain their original transport and override behavior.
    if isinstance(job_input, dict) and "mode" not in job_input:
        if os.environ.get("ALLOW_CUSTOM_WORKFLOWS", "true").lower() != "true":
            return {"error": "Custom workflows are disabled; supply a named mode", "error_code": "UNSUPPORTED_MODE"}
        with JOB_LOCK:
            return legacy_handler(job)
    input_root = Path(os.environ.get("COMFY_INPUT_DIR", "/comfyui/input")) / "ltx_jobs"
    output_root = Path(os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output")) / "ltx_jobs"
    token = secrets.token_hex(16)
    input_dir, output_dir = input_root / token, output_root / token
    queued = False
    result = None
    with JOB_LOCK:
        try:
            spec, params, media, workflow = prepare_input(job_input)
            values = dict(params, duration=params.get("num_frames", 121) / params.get("fps", 24))
            for key, targets in spec.get("bindings", {}).items():
                for target in targets:
                    value = values[key]
                    if "multiplier" in target:
                        value *= target["multiplier"]
                    _set(workflow, target, value)
            for role, source in media.items():
                binding = spec["media"][role]
                path = ingest(source, binding["kind"], input_dir, role, params,
                              require_audio=binding.get("require_audio", False),
                              ensure_audio=binding.get("ensure_audio", False))
                _set(workflow, binding, path.relative_to(input_root.parent).as_posix())
            prefix = f"ltx_jobs/{token}/LTX25"
            for target in spec["output_prefix"]:
                _set(workflow, target, prefix)
            prepared_job = dict(job, input={"workflow": workflow})
            queued = True
            result = legacy_handler(prepared_job, scan_text_artifacts=False)
            if isinstance(result, dict) and result.get("success") is True:
                expected_key = {"video": "videos", "audio": "audio", "image": "images"}[spec.get("output_kind", "video")]
                if not result.get(expected_key):
                    result.pop("success", None)
                    result.pop("status", None)
                    result["error"] = f"Workflow completed without a generated {spec.get('output_kind', 'video')} artifact"
                    result["error_code"] = "MISSING_OUTPUT"
                elif len(json.dumps(result).encode("utf-8")) > int(os.environ.get("MAX_RESULT_BYTES", 8 * 1024**2)):
                    # Keep already-uploaded URLs, exclude oversized inline blobs.
                    result = {"error": "Generated response exceeds MAX_RESULT_BYTES; configure S3 output uploads", "error_code": "RESULT_TOO_LARGE", "prompt_id": result.get("prompt_id"), "credit_usage": result.get("credit_usage", {})}
            return result
        except InputError as exc:
            result = {"error": str(exc), "error_code": exc.code}
            return result
        except Exception:
            # Never expose input base64, signed URLs, or internals to callers/logs.
            result = {"error": "Worker failed while preparing or executing the workflow", "error_code": "WORKER_ERROR"}
            if queued:
                result["refresh_worker"] = True
            return result
        finally:
            # A failed/disconnected execution may still be reading inputs. Recycle
            # that worker; leave its evidence intact until the container is removed.
            succeeded = isinstance(result, dict) and result.get("success") is True and not result.get("error")
            if not queued or succeeded:
                try:
                    _remove_job_directory(input_root, input_dir)
                    if succeeded:
                        _remove_job_directory(output_root, output_dir)
                except OSError:
                    if isinstance(result, dict):
                        result["refresh_worker"] = True
            elif isinstance(result, dict):
                result["refresh_worker"] = True
