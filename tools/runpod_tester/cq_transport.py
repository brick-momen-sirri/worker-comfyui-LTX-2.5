"""Trusted CQ graph transport for existing workers with legacy workflows enabled.

The local server validates and normalizes input exactly as the named-mode worker
does. This does not enable custom workflows on the remote endpoint, accept caller
graphs, change model files, or modify its result-delivery contract.
"""
import base64
import json
from pathlib import Path
import tempfile
import uuid

from ltx_worker.adapter import _set, prepare_input
from ltx_worker.errors import InputError

ROOT = Path(__file__).resolve().parents[2]


def normalize_video(source, directory, params):
    from ltx_worker.media import ingest
    return ingest(source, "video", directory, "video", params, ensure_audio=True)


def compile_workflow(job_input):
    if not isinstance(job_input, dict) or job_input.get("mode") != "video_enhance_cq_v2":
        raise InputError("Only the bundled CQ V2 mode uses CQ workflow transport")
    manifest = json.loads((ROOT / "workflows/manifest.cq-v2.json").read_text(encoding="utf-8"))
    spec, params, media, graph = prepare_input(job_input, manifest=manifest)
    for name, bindings in spec["bindings"].items():
        for binding in bindings:
            _set(graph, binding, params[name])
    token = uuid.uuid4().hex
    filename = f"ltxcq-{token}.mp4"
    # Normalize before transport: legacy media upload does not resize, trim,
    # validate duration, or add silence for the graph's original-audio output.
    with tempfile.TemporaryDirectory(prefix="ltxcq-") as directory:
        path = normalize_video(media["video"], directory, params)
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    _set(graph, spec["media"]["video"], filename)
    for binding in spec["output_prefix"]:
        _set(graph, binding, f"ltxcq/{token}/LTX25_CQ_{params['width']}x{params['height']}")
    return {"workflow": graph, "videos": [{"name": filename, "url": encoded}]}
