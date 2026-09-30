"""Standalone official DFR worker; uses the customized worker's S3 result contract."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import runpod
from ltx_worker.dfr import execute


def handler(job):
    return execute(job, progress=runpod.serverless.progress_update)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler, "concurrency_modifier": lambda _: 1})
