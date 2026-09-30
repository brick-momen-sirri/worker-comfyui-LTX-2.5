"""Test a running local worker without loading LTX models.

Start the worker with SERVE_API_LOCALLY=true, ALLOW_CUSTOM_WORKFLOWS=true,
and no S3 credentials/BUCKET_ENDPOINT_URL. This host-side test waits for the
Runpod development API, checks rejected input, and sends a generated 32x32
image through LoadImage -> SaveImage. It does not test LTX inference or S3.

Example:
  python examples/smoke_local_worker.py --url http://127.0.0.1:8000 \
      --report artifacts/local-worker-smoke.json
"""

import argparse
import base64
from datetime import datetime, timezone
import io
import ipaddress
import json
import math
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from PIL import Image


MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class SmokeError(RuntimeError):
    """An expected test failure with a safe, bounded explanation."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SmokeError("Local worker unexpectedly redirected the request")


def local_origin(value):
    """Limit this development test to an explicit loopback HTTP origin."""
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        is_local = hostname == "localhost"
        if hostname and not is_local:
            is_local = ipaddress.ip_address(hostname).is_loopback
        if (parsed.scheme not in ("http", "https") or not is_local
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query
                or parsed.fragment):
            raise ValueError()
        # Accessing port also rejects malformed/out-of-range port values.
        parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--url must be a loopback HTTP(S) origin, such as "
            "http://127.0.0.1:8000"
        ) from None
    return value.rstrip("/")


def positive_seconds(value):
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError()
        return seconds
    except ValueError:
        raise argparse.ArgumentTypeError("Timeout must be positive and finite") from None


def require(condition, message):
    if not condition:
        raise SmokeError(message)


def request_json(opener, url, timeout, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    require(len(raw) <= MAX_RESPONSE_BYTES, "API response exceeded 4 MiB")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SmokeError("API response was not valid JSON") from None
    require(isinstance(document, dict), "API response was not a JSON object")
    return document


def wait_ready(opener, origin, timeout):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SmokeError("Local Runpod API did not become ready before timeout")
        try:
            schema = request_json(
                opener, origin + "/openapi.json", min(5, remaining)
            )
            paths = schema.get("paths", {})
            if isinstance(paths, dict) and "post" in paths.get("/runsync", {}):
                return
        except (OSError, SmokeError):
            pass
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def run_smoke(origin, startup_timeout, request_timeout, report):
    # Avoid system proxies and redirects moving a local test outside loopback.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect()
    )
    report["stage"] = "readiness"
    wait_ready(opener, origin, startup_timeout)
    report["api_ready"] = True

    report["stage"] = "invalid_input"
    rejected = request_json(opener, origin + "/runsync", request_timeout, {
        "input": {
            "mode": "image_to_video",
            "prompt": "Validation smoke test",
            "media": {},
        }
    })
    require(rejected.get("status") == "FAILED", "Missing image was not rejected")
    require(
        rejected.get("error") == "Missing required media input 'image'",
        "Missing image did not produce the expected validation error",
    )
    require(isinstance(rejected.get("id"), str) and bool(rejected["id"]),
            "Rejected job did not retain a Runpod job identifier")
    # SDK 1.7.13's development /runsync omits output.error_code on failure.
    report["invalid_input"] = {
        "id": rejected["id"], "status": rejected["status"],
        "error": rejected["error"],
    }

    report["stage"] = "image_transport"
    token = uuid.uuid4().hex
    filename = "transport-" + token + ".png"
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buffer, format="PNG")
    image_data = base64.b64encode(buffer.getvalue()).decode("ascii")
    payload = {"input": {
        "workflow": {
            "load": {"class_type": "LoadImage", "inputs": {"image": filename}},
            "save": {"class_type": "SaveImage", "inputs": {
                "images": ["load", 0], "filename_prefix": "transport-" + token,
            }},
        },
        "images": [{
            "name": filename, "image": "data:image/png;base64," + image_data,
        }],
    }}
    result = request_json(opener, origin + "/runsync", request_timeout, payload)
    require(result.get("status") == "COMPLETED", "Image transport job failed")
    require(isinstance(result.get("id"), str) and bool(result["id"]),
            "Completed job did not retain a Runpod job identifier")
    output = result.get("output")
    require(isinstance(output, dict), "Completed job had no output object")
    require(output.get("success") is True, "Handler did not report success")
    require(isinstance(output.get("prompt_id"), str) and bool(output["prompt_id"]),
            "Handler response had no ComfyUI prompt identifier")
    require(isinstance(output.get("credit_usage"), dict),
            "Handler response did not retain credit_usage")
    images = output.get("images")
    require(isinstance(images, list) and len(images) == 1,
            "Expected exactly one returned output image")
    artifact = images[0]
    require(isinstance(artifact, dict) and artifact.get("type") == "base64",
            "Expected inline base64 output; run this smoke without S3 configuration")
    require(isinstance(artifact.get("data"), str), "Returned image data was missing")
    try:
        decoded = base64.b64decode(artifact["data"], validate=True)
        with Image.open(io.BytesIO(decoded)) as returned:
            returned.load()
            require(returned.size == (32, 32), "Returned image dimensions changed")
            require(returned.convert("RGB").getpixel((0, 0)) == (10, 20, 30),
                    "Returned image pixel did not match the uploaded image")
    except (ValueError, OSError):
        raise SmokeError("Returned image was not valid base64 image data") from None
    report["image_transport"] = {
        "id": result["id"], "status": result["status"],
        "prompt_id": output["prompt_id"], "image_size": [32, 32],
        "pixel_verified": True, "credit_usage_present": True,
        "input_format": "base64_data_uri", "output_format": "base64",
    }
    report["stage"] = "complete"
    report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=local_origin, default="http://127.0.0.1:8000")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--startup-timeout", type=positive_seconds, default=180)
    parser.add_argument("--request-timeout", type=positive_seconds, default=90)
    args = parser.parse_args()
    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "url": args.url, "passed": False, "api_ready": False,
        "ltx_inference": "not run", "s3_delivery": "not tested",
        "scope": "Local readiness, invalid input, and legacy image transport only",
    }
    started = time.monotonic()
    try:
        run_smoke(args.url, args.startup_timeout, args.request_timeout, report)
    except SmokeError as exc:
        report["error"] = str(exc)
    except urllib.error.HTTPError as exc:
        report["error"] = f"Local API returned HTTP {exc.code}"
    except OSError:
        report["error"] = "Local API request failed or timed out"
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    summary = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(summary, encoding="utf-8")
    print(summary, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
