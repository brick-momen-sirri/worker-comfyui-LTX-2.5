"""Exercise the pinned ComfyUI DynamicCombo parser without GPU dependencies.

Only schema constructors and pure Python input expansion are evaluated. This is
not a substitute for importing the complete node pack or generating a video.
"""

import argparse
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace


class Input:
    def __init__(self, kind, id, **options):
        self.kind, self.id, self.options = kind, id, options


class Kind:
    def __init__(self, name):
        self.name = name

    def Input(self, id, **options):
        return Input(self.name, id, **options)

    def Output(self, *args, **kwargs):
        return None

    def Option(self, key, inputs):
        return {"key": key, "inputs": to_v1(inputs)}


def to_v1(inputs):
    result = {"required": {}, "optional": {}}
    for item in inputs:
        options = dict(item.options)
        optional = options.pop("optional", False)
        kind = "COMFY_DYNAMICCOMBO_V3" if item.kind == "DynamicCombo" else item.kind.upper()
        result["optional" if optional else "required"][item.id] = (kind, options)
    return result


def extract(path, function_names=(), methods=()):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    output = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in function_names:
            output.append(copy.deepcopy(node))
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and (node.name, child.name) in methods:
                    function = copy.deepcopy(child)
                    function.decorator_list = []
                    function.name = node.name + "_" + function.name
                    output.append(function)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-source", type=Path, required=True)
    parser.add_argument("--workflows", type=Path, default=Path(__file__).resolve().parents[1] / "workflows")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.comfy_source
    functions = extract(source / "comfy_api/latest/_io.py",
                        ("handle_prefix", "finalize_prefix", "parse_class_inputs", "get_dynamic_input_func",
                         "get_finalized_class_inputs", "build_nested_inputs"),
                        (("DynamicCombo", "_expand_schema_for_dynamic"),))
    functions += extract(source / "comfy_extras/nodes_video.py", ("_save_video_codec_input",), (("SaveVideo", "define_schema"),))
    functions += extract(source / "comfy_extras/nodes_audio.py", methods=(("SaveAudioAdvanced", "define_schema"),))
    tree = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions], type_ignores=[])
    api = SimpleNamespace(**{name: Kind(name) for name in ("Video", "Audio", "Float", "Combo", "String", "DynamicCombo")})
    api.Schema = lambda **kwargs: kwargs
    api.Hidden = SimpleNamespace(prompt="PROMPT", extra_pnginfo="EXTRA_PNGINFO")
    env = {"io": api, "IO": api, "DynamicPathsDefaultValue": SimpleNamespace(EMPTY_DICT="empty_dict")}
    exec(compile(ast.fix_missing_locations(tree), "pinned_comfyui_schema_excerpt", "exec"), env)
    env["DYNAMIC_INPUT_LOOKUP"] = {"COMFY_DYNAMICCOMBO_V3": env["DynamicCombo__expand_schema_for_dynamic"]}
    manifest = json.loads((args.workflows / "manifest.json").read_text())
    checks = {}
    for mode, meta in manifest["modes"].items():
        graph = json.loads((args.workflows / meta["file"]).read_text())
        node = graph["save"]
        fn = env[node["class_type"] + "_define_schema"]
        schema = fn(None)
        inputs = node["inputs"]
        finalized, _, v3_data = env["get_finalized_class_inputs"](to_v1(schema["inputs"]), inputs)
        allowed = set(finalized["required"]) | set(finalized["optional"])
        unknown = set(inputs) - allowed
        missing = set(finalized["required"]) - set(inputs)
        assert not unknown, (mode, "unknown", unknown)
        assert not missing, (mode, "missing", missing)
        nested = env["build_nested_inputs"](inputs, v3_data)
        if node["class_type"] == "SaveVideo":
            assert nested["format"] == {"format": "mp4", "codec": {"codec": "h264", "encoding": {"encoding": "auto"}}}, nested
        else:
            assert nested["format"] == {"format": "flac"}, nested
        checks[mode] = {"class_type": node["class_type"], "expanded_format": nested["format"], "passed": True}
    result = {"generation_executed": False, "full_comfyui_imported": False,
              "method": "Execute AST-extracted official schema constructors and DynamicCombo expansion functions with input-record stubs", "checks": checks}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
