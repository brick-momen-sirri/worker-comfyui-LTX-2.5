"""Bounded startup wait with dead-process detection and local workflow checks."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def wait_ready(url, timeout, pid_file):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_file and Path(pid_file).exists():
            try:
                os.kill(int(Path(pid_file).read_text().strip()), 0)
            except (ProcessLookupError, ValueError):
                raise RuntimeError("ComfyUI exited during startup; inspect the startup log") from None
        try:
            with urlopen(url + "/object_info", timeout=5) as response:
                nodes = json.load(response)
            if "UNETLoader" in nodes and "CLIPLoader" in nodes:
                return nodes
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError(f"ComfyUI was not ready within {timeout:g} seconds")


def verify_credit_hooks():
    status_path = Path("/comfyui/custom_nodes/comfyui_credit_tracker/tracker_status.json")
    status = json.loads(status_path.read_text())
    details = status.get("details", {})
    if status.get("event") != "registered" or not details.get("prompt_hook") or not details.get("server_credit_capture"):
        raise RuntimeError("Preserved credit tracker failed to register its prompt/credit hooks")
    targets = details.get("patch_targets", {})
    if not targets or not all(targets.values()):
        raise RuntimeError("Pinned ComfyUI does not expose every required credit-tracker hook")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--pid-file", default="/tmp/comfyui.pid")
    parser.add_argument("--validate-workflows", action="store_true")
    args = parser.parse_args()
    nodes = wait_ready(args.url, args.timeout, args.pid_file)
    missing = [name for name in ("CreditTrackerLogger", "CreditTrackerReportViewer") if name not in nodes]
    if missing:
        raise RuntimeError("Preserved credit tracker failed to import: " + ", ".join(missing))
    verify_credit_hooks()
    if args.validate_workflows:
        subprocess.run([
            sys.executable, "/scripts/validate_workflows.py", "--object-info", args.url + "/object_info",
            "--models-present", "--model-directory", "/comfyui/models",
        ], check=True)
    print(f"ComfyUI ready: {len(nodes)} registered nodes", flush=True)
