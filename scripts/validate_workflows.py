"""Validate graphs without model weights, optionally against source or live ComfyUI.

Examples:
  python scripts/validate_workflows.py
  python scripts/validate_workflows.py --comfy-source .research/comfyui --ltx-source .research/ltx
  python scripts/validate_workflows.py --object-info http://127.0.0.1:8188/object_info

This validates API graph structure and schemas; it does NOT execute diffusion.
"""

import argparse
import ast
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODEL_FIELDS = {"unet_name": "diffusion_models", "clip_name": "text_encoders", "vae_name": "vae",
                "lora_name": "loras", "model_name": "latent_upscale_models"}
MEDIA_FIELDS = {"LoadImage": "image", "LoadVideo": "file", "LoadAudio": "audio"}


def constant(node, default=None):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return default


def kwargs(call):
    return {item.arg: item.value for item in call.keywords}


def ast_input(call):
    """Read a V3 schema input declaration without importing torch or ComfyUI."""
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr != "Input":
        return None
    options = kwargs(call)
    name = constant(call.args[0]) if call.args else constant(options.get("id"))
    if not isinstance(name, str):
        # MultiType wraps a real input declaration.
        return ast_input(call.args[0]) if call.args else None
    kind = call.func.value.attr if isinstance(call.func.value, ast.Attribute) else "Custom"
    return name, {"optional": constant(options.get("optional"), False), "type": kind,
                  "minimum": constant(options.get("min")), "maximum": constant(options.get("max")),
                  "enum": constant(options.get("options")) if kind == "Combo" else None}


def source_schemas(paths):
    result = {}
    aliases = {}
    for directory in paths:
        if not directory.is_dir():
            raise ValueError(f"Missing source directory: {directory}")
        for path in directory.rglob("*.py"):
            if ".git" in path.parts or "site-packages" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            except (SyntaxError, UnicodeError):
                continue
            for node in tree.body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                    if any(isinstance(t, ast.Name) and t.id == "NODE_CLASS_MAPPINGS" for t in node.targets):
                        for key, value in zip(node.value.keys, node.value.values):
                            if isinstance(value, ast.Name) and isinstance(constant(key), str):
                                aliases[constant(key)] = value.id
                if not isinstance(node, ast.ClassDef):
                    continue
                entry = {"source": f"{path}:{node.lineno}", "inputs": {}, "outputs": None}
                node_id = node.name
                for child in node.body:
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name) and target.id == "RETURN_TYPES":
                                outputs = constant(child.value)
                                if isinstance(outputs, (tuple, list)):
                                    entry["outputs"] = len(outputs)
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if child.name == "define_schema":
                        for call in ast.walk(child):
                            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "Schema":
                                options = kwargs(call)
                                node_id = constant(options.get("node_id"), node.name)
                                inputs = options.get("inputs")
                                if isinstance(inputs, ast.List):
                                    for item in inputs.elts:
                                        parsed = ast_input(item)
                                        if parsed:
                                            entry["inputs"][parsed[0]] = parsed[1]
                                outputs = options.get("outputs")
                                if isinstance(outputs, ast.List):
                                    entry["outputs"] = len(outputs.elts)
                                entry["output_node"] = constant(options.get("is_output_node"), False)
                    if child.name == "INPUT_TYPES":
                        for ret in ast.walk(child):
                            if not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.Dict):
                                continue
                            for key, value in zip(ret.value.keys, ret.value.values):
                                group = constant(key)
                                if group not in ("required", "optional") or not isinstance(value, ast.Dict):
                                    continue
                                for ikey, ivalue in zip(value.keys, value.values):
                                    name = constant(ikey)
                                    if isinstance(name, str):
                                        first = constant(ivalue.elts[0]) if isinstance(ivalue, ast.Tuple) else None
                                        details = constant(ivalue.elts[1], {}) if isinstance(ivalue, ast.Tuple) and len(ivalue.elts) > 1 else {}
                                        entry["inputs"][name] = {"optional": group == "optional", "type": first,
                                                                 "minimum": details.get("min"), "maximum": details.get("max")}
                if entry["inputs"] or entry["outputs"] is not None:
                    result[node_id] = entry
                    result.setdefault(node.name, entry)
    for alias, name in aliases.items():
        if name in result:
            result[alias] = result[name]
    return result


def live_schemas(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        info = json.load(response)
    result = {}
    for name, data in info.items():
        entry = {"source": url, "inputs": {}, "outputs": len(data.get("output", [])), "output_node": data.get("output_node", False)}
        for group in ("required", "optional"):
            for key, spec in data.get("input", {}).get(group, {}).items():
                details = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
                entry["inputs"][key] = {"optional": group == "optional", "type": spec[0], "minimum": details.get("min"),
                                         "maximum": details.get("max"), "enum": spec[0] if isinstance(spec[0], list) else None}
        result[name] = entry
    return result


def validate(folder, schemas=None, models_present=False, model_directory=None):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    model_manifest = json.loads((ROOT / "models" / "manifest.json").read_text(encoding="utf-8"))
    model_paths = {m["destination"] for m in model_manifest["models"]}
    errors, mode_reports, used_models, used_classes = [], {}, set(), set()
    for mode, meta in manifest["modes"].items():
        graph = json.loads((folder / meta["file"]).read_text(encoding="utf-8"))
        edges = {}
        for node_id, node in graph.items():
            prefix = f"{mode}/{node_id}"
            cls, inputs = node.get("class_type"), node.get("inputs")
            if not isinstance(cls, str) or not isinstance(inputs, dict):
                errors.append(f"{prefix}: not an API-format node")
                continue
            used_classes.add(cls)
            edges[node_id] = []
            schema = schemas.get(cls) if schemas else None
            if schemas and schema is None:
                errors.append(f"{prefix}: class {cls} is not registered in inspected schema")
            if schema:
                required = {k for k, v in schema["inputs"].items() if not v["optional"]}
                missing = required - inputs.keys()
                if missing:
                    errors.append(f"{prefix}: missing required inputs {sorted(missing)}")
            for field, value in inputs.items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                    edges[node_id].append(value[0])
                    if value[0] not in graph:
                        errors.append(f"{prefix}.{field}: link to missing {value[0]}")
                    elif schemas and graph[value[0]]["class_type"] in schemas:
                        count = schemas[graph[value[0]]["class_type"]]["outputs"]
                        if count is not None and not 0 <= value[1] < count:
                            errors.append(f"{prefix}.{field}: invalid output slot {value}")
                    continue
                if field in MODEL_FIELDS:
                    path = MODEL_FIELDS[field] + "/" + value
                    used_models.add(path)
                    if path not in model_paths:
                        errors.append(f"{prefix}.{field}: model absent from build inventory: {path}")
                    if model_directory and not (model_directory / path).is_file():
                        errors.append(f"{prefix}.{field}: missing local model {path}")
                if schema:
                    if field not in schema["inputs"] and "." not in field:
                        errors.append(f"{prefix}: unknown input {field}")
                    spec = schema["inputs"].get(field, {})
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if spec.get("minimum") is not None and value < spec["minimum"]:
                            errors.append(f"{prefix}.{field}: below source minimum")
                        if spec.get("maximum") is not None and value > spec["maximum"]:
                            errors.append(f"{prefix}.{field}: above source maximum")
                    enum = spec.get("enum")
                    # Template media placeholders are replaced only when a job arrives.
                    # Never require them to exist when checking the baked image.
                    check_enum = cls not in MEDIA_FIELDS and (field not in MODEL_FIELDS or models_present)
                    if isinstance(enum, list) and enum and not any(isinstance(x, dict) for x in enum) and check_enum and value not in enum:
                        errors.append(f"{prefix}.{field}: {value!r} not in registered options")
        for param, bindings in meta["bindings"].items():
            if param not in meta["defaults"] and param != "duration":
                errors.append(f"{mode}: parameter {param} has no default or supported derivation")
            for binding in bindings:
                if binding["node_id"] not in graph or binding["input"] not in graph[binding["node_id"]]["inputs"]:
                    errors.append(f"{mode}: invalid parameter binding {param}: {binding}")
        for role, binding in meta["media"].items():
            node = graph.get(binding["node_id"], {})
            if MEDIA_FIELDS.get(node.get("class_type")) != binding["input"]:
                errors.append(f"{mode}: media {role} does not bind a matching loader")
        visiting, visited = set(), set()
        def visit(node):
            if node in visiting:
                errors.append(f"{mode}: dependency cycle at {node}")
                return
            if node in visited:
                return
            visiting.add(node)
            for parent in edges.get(node, []):
                visit(parent)
            visiting.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
        mode_reports[mode] = {"nodes": len(graph), "media": sorted(meta["media"]), "status": "schema_checked" if schemas else "structure_checked"}
    report = {"generation_executed": False, "models_loaded": False, "mode_count": len(mode_reports),
              "unique_classes": len(used_classes), "referenced_models": sorted(used_models), "modes": mode_reports,
              "errors": errors, "success": not errors}
    if schemas:
        report["node_sources"] = {name: schemas[name]["source"] for name in sorted(used_classes) if name in schemas}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflows", type=Path, default=ROOT / "workflows")
    parser.add_argument("--comfy-source", type=Path)
    parser.add_argument("--ltx-source", type=Path)
    parser.add_argument("--object-info", help="URL of running ComfyUI's /object_info endpoint")
    parser.add_argument("--models-present", action="store_true", help="Also require model filenames in loader dropdowns")
    parser.add_argument("--model-directory", type=Path, help="Also verify files exist under this ComfyUI models directory")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    schemas = None
    if args.object_info:
        schemas = live_schemas(args.object_info)
    elif args.comfy_source or args.ltx_source:
        schemas = source_schemas([p for p in (args.comfy_source, args.ltx_source) if p])
    report = validate(args.workflows, schemas, args.models_present, args.model_directory)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
