"""Compile only bundled 4K recipes for the original worker's workflow contract.

The current deployed image already contains their nodes and weights. This local
bridge never accepts caller-supplied graphs or changes Runpod result delivery.
"""

import base64
from pathlib import Path
import re
import sys
import urllib.parse
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ltx_worker.adapter import prepare_input, _set
from ltx_worker.errors import InputError

MODES = {"image_to_video_4k", "video_upscale_4k", "image_to_video_native_4k"}


def media_payload(source):
    if isinstance(source, dict):
        if len(source) != 1 or not set(source) <= {"url", "base64"}:
            raise InputError("Use exactly one URL or base64 value for each 4K input")
        source = next(iter(source.values()))
    if not isinstance(source, str) or not source:
        raise InputError("The 4K media input is missing")
    if source.startswith("https://"):
        try:
            parsed = urllib.parse.urlsplit(source)
            if (not parsed.hostname or parsed.port not in (None, 443)
                    or parsed.username is not None or parsed.password is not None or parsed.fragment):
                raise ValueError()
        except ValueError:
            raise InputError("Use a valid HTTPS media URL without embedded credentials") from None
        return source
    if source.startswith("data:"):
        match = re.fullmatch(r"data:[^;,]+;base64,([A-Za-z0-9+/=\r\n]+)", source)
        if not match:
            raise InputError("The 4K media data URI is invalid")
        encoded = match[1]
    else:
        encoded = source
    try:
        if not base64.b64decode(encoded, validate=True):
            raise ValueError()
    except ValueError:
        raise InputError("Use valid base64 media or an HTTPS URL") from None
    return source


def compile_workflow(job_input):
    if not isinstance(job_input, dict) or job_input.get("mode") not in MODES:
        raise InputError("Only the bundled 4K modes use workflow compatibility transport")
    spec, params, media, graph = prepare_input(job_input)
    token = uuid.uuid4().hex
    values = dict(params, duration=params["num_frames"] / params["fps"])
    for name, bindings in spec["bindings"].items():
        for binding in bindings:
            _set(graph, binding, values[name] * binding.get("multiplier", 1))
    result = {"workflow": graph}
    for role, source in media.items():
        binding = spec["media"][role]
        kind = binding["kind"]
        # ComfyUI detects the media container; filenames are isolated per job.
        filename = f"ltx4k-{token}-{role}" + (".png" if kind == "image" else ".mp4")
        _set(graph, binding, filename)
        result.setdefault("images" if kind == "image" else "videos", []).append(
            {"name": filename, "url": media_payload(source)})
    label = "LTX25_NATIVE_4K" if job_input["mode"] == "image_to_video_native_4k" else "LTX25_4K"
    for binding in spec["output_prefix"]:
        _set(graph, binding, f"ltx4k/{token}/{label}")
    return result
