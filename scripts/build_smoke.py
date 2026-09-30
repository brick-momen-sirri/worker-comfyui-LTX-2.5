"""CPU-only node import check during Docker build; this does not generate media."""
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, "/scripts")
from wait_for_comfy import wait_ready, verify_credit_hooks


log_path = Path("/tmp/comfy-build-smoke.log")
env = dict(os.environ, RUNPOD_POD_ID="build-validation", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
with log_path.open("w") as log:
    process = subprocess.Popen([
        sys.executable, "main.py", "--cpu", "--listen", "127.0.0.1", "--port", "8189",
        "--disable-auto-launch", "--disable-metadata", "--cache-none", "--log-stdout",
    ], cwd="/comfyui", stdout=log, stderr=subprocess.STDOUT, env=env)
    Path("/tmp/comfy-build-smoke.pid").write_text(str(process.pid))
    try:
        nodes = wait_ready("http://127.0.0.1:8189", 300, "/tmp/comfy-build-smoke.pid")
        required = {"CreditTrackerLogger", "CreditTrackerReportViewer", "LTXVAddGuideAdvanced"}
        missing = required - nodes.keys()
        if missing:
            raise RuntimeError(f"Required custom nodes failed import: {sorted(missing)}")
        verify_credit_hooks()
        subprocess.run([
            sys.executable, "/scripts/validate_workflows.py", "--object-info", "http://127.0.0.1:8189/object_info",
        ], cwd="/", env=env, check=True)
        print(f"CPU build smoke passed: {len(nodes)} nodes registered; no weights loaded")
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        print(log_path.read_text(errors="replace"))
    if "(IMPORT FAILED)" in log_path.read_text(errors="replace"):
        raise RuntimeError("ComfyUI reported a node import failure")
