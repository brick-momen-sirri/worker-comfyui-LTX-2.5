"""Select a complete, immutable LTX 2.5 image profile at build time.

The standard INT8/BF16 profiles share a workflow catalog and differ in their
transformer and Gemma encoder. The CQ V2 profile has its own smaller catalog and
the exact dev-model, convolution-VAE, and LoRA bundle required by that recipe.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ("int8", "bf16", "cq-v2")
LOADER_FIELDS = {
    "UNETLoader": ("unet_name", "diffusion_models"),
    "CLIPLoader": ("clip_name", "text_encoders"),
}


def profile_loaders(inventory: dict) -> dict[str, str]:
    """Require exactly one model for each of the two switchable loader roles."""
    selected = {}
    for _, directory in LOADER_FIELDS.values():
        names = [
            PurePosixPath(entry["destination"]).name
            for entry in inventory["models"]
            if PurePosixPath(entry["destination"]).parent.as_posix() == directory
        ]
        if len(names) != 1:
            raise ValueError(f"Profile must contain exactly one {directory} model")
        selected[directory] = names[0]
    return selected


def write_json(path: Path, document: dict) -> None:
    """Replace individual files atomically after all input validation succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
        # Windows indexers and antivirus can briefly hold a newly written JSON
        # file. Preserve atomic replacement while tolerating that transient lock.
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_profile(root: Path, profile: str) -> dict:
    """Keep the active model inventories and every shipped graph in agreement."""
    if profile not in PROFILES:
        raise ValueError(f"Unsupported model profile {profile!r}; choose int8, bf16, or cq-v2")
    root = Path(root).resolve()
    inventories, loader_names = {}, {}
    for name in PROFILES:
        path = root / "models" / "profiles" / (name + ".json")
        inventory = json.loads(path.read_text(encoding="utf-8"))
        if inventory.get("schema_version") != 1 or inventory.get("precision") != name:
            raise ValueError(f"Invalid model inventory for {name}")
        inventories[name] = inventory
        loader_names[name] = profile_loaders(inventory)

    folder = root / "workflows"
    manifest_path = folder / "manifest.json"
    # The active manifest is overwritten in an image build. Keep immutable source
    # manifests so switching a working tree from cq-v2 back to a standard profile
    # cannot accidentally retain the CQ-only contract.
    source_manifest = folder / ("manifest.cq-v2.json" if profile == "cq-v2" else "manifest.standard.json")
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not manifest.get("modes"):
        raise ValueError("Unsupported or empty workflow manifest")
    writes = {}
    for mode, spec in manifest["modes"].items():
        relative = PurePosixPath(spec["file"])
        path = (folder / spec["file"]).resolve()
        if (relative.is_absolute() or ".." in relative.parts
                or "\\" in spec["file"] or not path.is_relative_to(folder)
                or path.suffix != ".json"):
            raise ValueError(f"Unsafe workflow path for {mode}")
        graph = json.loads(path.read_text(encoding="utf-8"))
        for node in graph.values():
            field_info = LOADER_FIELDS.get(node.get("class_type"))
            if not field_info:
                continue
            field, directory = field_info
            known_names = {names[directory] for names in loader_names.values()}
            if node["inputs"].get(field) in known_names:
                node["inputs"][field] = loader_names[profile][directory]
        writes[path] = graph
    manifest["precision"] = profile
    writes[manifest_path] = manifest
    writes[root / "models" / "manifest.json"] = inventories[profile]
    # Existing downloader/startup integration reads this path inside the image.
    writes[root / "models-manifest.json"] = inventories[profile]
    for path, document in writes.items():
        write_json(path, document)
    return {
        "profile": profile,
        "workflows": len(manifest["modes"]),
        "models": len(inventories[profile]["models"]),
        "model_bytes": sum(entry["size"] for entry in inventories[profile]["models"]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="int8")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    print(json.dumps(configure_profile(args.root, args.profile)))


if __name__ == "__main__":
    main()
