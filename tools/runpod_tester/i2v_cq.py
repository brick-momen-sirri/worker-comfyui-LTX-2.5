"""Opt-in I2V experiment with the weights already installed in the CQ image.

CQ's publisher documents reference-video restoration, not ordinary I2V. This
local-only mode compiles a trusted graph for the existing legacy worker API.
"""
import base64
import json
from pathlib import Path
import tempfile
import uuid

from ltx_worker.adapter import _set, prepare_input

ROOT = Path(__file__).resolve().parents[2]
MODE = "image_to_video_cq_experimental"


def manifest():
    return json.loads((ROOT / "workflows/experimental/manifest.i2v-cq.json").read_text(encoding="utf-8"))


def compile_workflow(job_input):
    from ltx_worker.media import ingest

    spec, params, media, graph = prepare_input(job_input, manifest=manifest())
    for name, bindings in spec["bindings"].items():
        for binding in bindings:
            _set(graph, binding, params[name])
    token = uuid.uuid4().hex
    filename = f"ltxi2vcq-{token}.png"
    with tempfile.TemporaryDirectory(prefix="ltxi2vcq-") as directory:
        path = ingest(media["image"], "image", directory, "image", params)
        encoded = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    _set(graph, spec["media"]["image"], filename)
    for binding in spec["output_prefix"]:
        _set(graph, binding, f"ltxi2vcq/{token}/I2V_CQ_{params['cq_lora_strength']:g}")
    return {"workflow": graph, "images": [{"name": filename, "image": encoded}]}
