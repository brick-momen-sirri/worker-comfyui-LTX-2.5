"""Experimental endpoint-image guidance with the existing CQ image's weights."""
import base64
import json
from pathlib import Path
import tempfile
import uuid

from ltx_worker.adapter import _set, prepare_input

ROOT = Path(__file__).resolve().parents[2]
MODE = "first_last_frame_cq_experimental"


def manifest():
    return json.loads((ROOT / "workflows/experimental/manifest.first-last-cq.json").read_text(encoding="utf-8"))


def compile_workflow(job_input):
    from ltx_worker.media import ingest

    spec, params, media, graph = prepare_input(job_input, manifest=manifest())
    for name, bindings in spec["bindings"].items():
        for binding in bindings:
            _set(graph, binding, params[name])
    token = uuid.uuid4().hex
    images = []
    with tempfile.TemporaryDirectory(prefix="ltxflfcq-") as directory:
        for role, binding in spec["media"].items():
            path = ingest(media[role], "image", directory, role, params)
            filename = f"ltxflfcq-{token}-{role}.png"
            images.append({"name": filename, "image": base64.b64encode(Path(path).read_bytes()).decode("ascii")})
            _set(graph, binding, filename)
    for binding in spec["output_prefix"]:
        _set(graph, binding, f"ltxflfcq/{token}/FLF_CQ_{params['cq_lora_strength']:g}")
    return {"workflow": graph, "images": images}
