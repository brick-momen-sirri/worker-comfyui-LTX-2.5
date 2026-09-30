"""Fetch exact reviewed source commits; never use a branch or latest release."""
import json
from pathlib import Path
import shutil
import subprocess
import sys


for source in json.loads(Path(sys.argv[1]).read_text())["sources"]:
    target = Path(source["destination"])
    target.mkdir(parents=True, exist_ok=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(target), *args], text=True).strip()
    git("init")
    git("remote", "add", "origin", source["repository"])
    git("fetch", "--depth", "1", "origin", source["commit"])
    git("checkout", "--detach", "FETCH_HEAD")
    if git("rev-parse", "HEAD") != source["commit"]:
        raise RuntimeError(f"Source commit mismatch: {source['name']}")
    shutil.rmtree(target / ".git")
    print(f"Fetched {source['name']} at {source['commit']}", flush=True)
