import os
from pathlib import Path
from urllib.request import urlopen

for filename in ("/tmp/comfyui.pid", "/tmp/worker.pid"):
    os.kill(int(Path(filename).read_text().strip()), 0)
with urlopen("http://127.0.0.1:8188/system_stats", timeout=4) as response:
    assert response.status == 200
