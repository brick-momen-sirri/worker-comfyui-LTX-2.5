"""Local test page for the deployed worker. Python 3.10+; optional boto3 for S3."""

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
if str(STATIC.parent) not in sys.path:
    sys.path.insert(0, str(STATIC.parent))
import s3_upload
import four_k
MAX_BODY_BYTES = 10_000_000
MAX_RUN_BYTES = 9_000_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
API_ROOT = "https://api.runpod.ai/v2"


class TesterError(Exception):
    def __init__(self, message, status=400, submission_uncertain=False):
        super().__init__(message)
        self.status = status
        self.submission_uncertain = submission_uncertain


def normalize_endpoint(value):
    if not isinstance(value, str):
        raise TesterError("Enter a Runpod Serverless endpoint ID or API URL.")
    value = value.strip()
    if value.startswith("https://"):
        try:
            url = urllib.parse.urlsplit(value)
            if (url.hostname != "api.runpod.ai" or url.port not in (None, 443)
                    or url.username is not None or url.password is not None
                    or url.query or url.fragment):
                raise ValueError()
            match = re.fullmatch(r"/v2/([A-Za-z0-9_-]{1,128})(?:/(?:run|runsync|health))?/?", url.path)
            if not match:
                raise ValueError()
            value = match[1]
        except ValueError:
            raise TesterError("Use an endpoint ID or https://api.runpod.ai/v2/ENDPOINT_ID URL.") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise TesterError("Invalid endpoint ID. Use the Serverless endpoint, not a Pod URL.")
    return value


def validate_job_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise TesterError("A valid Runpod job ID is required.")
    return value


def normalize_runtime(value):
    if value not in ("comfyui", "cq-v2", "dfr"):
        raise TesterError("Tester runtime must be comfyui, cq-v2, or dfr.")
    return value


def dfr_module():
    # Validation and metadata are stdlib-only; model dependencies belong to the
    # separate DFR worker image, not to this local page.
    from ltx_worker import dfr
    return dfr


def get_config(endpoint_id="", api_key_configured=False, runtime="comfyui"):
    runtime = normalize_runtime(runtime)
    source = ({"modes": dfr_module().mode_specs()} if runtime == "dfr" else
              json.loads((ROOT / "workflows" / ("manifest.cq-v2.json" if runtime == "cq-v2" else "manifest.json")).read_text(encoding="utf-8")))
    experimental_i2v = runtime == "cq-v2" and os.environ.get("RUNPOD_TESTER_I2V_CQ_EXPERIMENT") == "1"
    if experimental_i2v:
        import i2v_cq
        source["modes"].update(i2v_cq.manifest()["modes"])
    if runtime == "cq-v2" and os.environ.get("RUNPOD_TESTER_FIRST_LAST_CQ_EXPERIMENT") == "1":
        import first_last_cq
        source["modes"].update(first_last_cq.manifest()["modes"])
    priority = (["image_to_video_dfr_4k", "text_to_video_dfr_4k"] if runtime == "dfr" else
                ["video_enhance_cq_v2"] if runtime == "cq-v2" else
                ["text_to_video", "image_to_video", "first_last_frame", "video_to_video",
                 "video_upscale_x2", "text_to_audio"])
    names = priority + [name for name in source["modes"] if name not in priority]
    labels = {"text_to_video": "Text to video", "image_to_video": "Image to video",
              "first_last_frame": "First and last frames", "video_to_video": "Video to video",
              "video_upscale_x2": "Video upscale 2×", "text_to_audio": "Text to audio",
              "video_enhance_cq_v2": "Video enhance · CQ V2",
              "image_to_video_cq_experimental": "Image to video + CQ · experimental",
              "first_last_frame_cq_experimental": "First and last frames + CQ · experimental"}
    modes = []
    for name in names:
        spec = source["modes"][name]
        defaults = copy.deepcopy(spec["defaults"])
        if runtime == "comfyui":
            defaults.update(num_frames=9, fps=24, seed=42)
        elif runtime == "cq-v2" and name == "video_enhance_cq_v2":
            # Start below the publisher's 153-frame cap for the first paid GPU test.
            defaults.update(num_frames=33, seed=42)
        if runtime == "comfyui" and "width" in defaults and spec.get("admission_profile") not in ("4k", "native_4k"):
            defaults.update(width=512, height=320)
            if name == "video_upscale_x2":
                defaults.update(width=256, height=256)
        rules = copy.deepcopy(spec["constraints"])
        # JSON numbers in the browser cannot safely represent all uint64 values.
        rules["seed"]["maximum"] = min(rules["seed"].get("maximum", 2**53 - 1), 2**53 - 1)
        modes.append({
            "id": name, "label": labels.get(name, name.replace("_", " ").capitalize()),
            "description": spec["description"],
            "media": [{"role": role, "kind": info["kind"], "required": info.get("required", True)}
                      for role, info in spec["media"].items()],
            "parameters": {"defaults": defaults, "rules": rules},
            "output_scale": spec.get("output_scale", 1), "output_kind": spec.get("output_kind"),
            "output_dimensions": spec.get("output_dimensions"),
            "admission_profile": spec.get("admission_profile"),
            "prompt_required": spec.get("prompt_required", True),
            "transport": ("workflow" if name in ("image_to_video_cq_experimental", "first_last_frame_cq_experimental") else
                          os.environ.get("RUNPOD_TESTER_4K_TRANSPORT", "workflow") if runtime == "comfyui" and name in four_k.MODES else "named"),
        })
    return {"endpoint_id": endpoint_id, "api_key_configured": bool(api_key_configured),
            "runtime": runtime, "validation_preset_available": runtime == "comfyui",
            "runtime_description": ("Official Python DFR: requires the separate DFR worker image. The tester cannot verify the remote image."
                                    if runtime == "dfr" else
                                    "ComfyUI CQ Enhancer V2: requires the separate cq-v2 worker image."
                                    if runtime == "cq-v2" else
                                    "ComfyUI worker and bundled ComfyUI workflows."),
            "modes": modes, "max_run_bytes": MAX_RUN_BYTES, "poll_interval_ms": 5000,
            "s3_upload": s3_upload.capabilities()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TesterError("Runpod returned an unexpected redirect; the API key was not forwarded.", 502,
                          submission_uncertain=req.get_method() == "POST" and req.full_url.endswith("/run"))


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise TesterError("The request must contain valid JSON values.") from None


def _decode_json(raw):
    def invalid_constant(_):
        raise ValueError()
    try:
        value = json.loads(raw, parse_constant=invalid_constant)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise TesterError("Expected a valid JSON object.") from None


def _redact_key(value, key):
    if isinstance(value, str):
        return value.replace(key, "[redacted]") if key else value
    if isinstance(value, dict):
        return {_redact_key(k, key): _redact_key(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_key(item, key) for item in value]
    return value


class RunpodClient:
    def __init__(self, api_key="", keep_key_in_memory=False, runtime="comfyui"):
        self.runtime = normalize_runtime(runtime)
        self.api_key = api_key
        self.keep_key_in_memory = keep_key_in_memory
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, action, endpoint_id, api_key="", job_id=None, job_input=None):
        if action not in ("health", "run", "status", "cancel"):
            raise TesterError("Unsupported Runpod action.")
        endpoint = normalize_endpoint(endpoint_id)
        if not isinstance(api_key, str):
            raise TesterError("The API key must be a string.")
        key = api_key or self.api_key
        if not isinstance(key, str) or not key.strip() or len(key) > 512 or not key.isascii() or re.search(r"\s", key):
            raise TesterError("Enter your Runpod API key, or set RUNPOD_API_KEY before starting the tester.", 401)
        path = f"{API_ROOT}/{endpoint}/{action}"
        method = "POST" if action in ("run", "cancel") else "GET"
        payload = None
        if action in ("status", "cancel"):
            path += "/" + validate_job_id(job_id)
        if action == "run":
            if not isinstance(job_input, dict):
                raise TesterError("The input field must be a JSON object.")
            # The diagnostic missing-media request intentionally reaches the worker.
            modes = {mode["id"] for mode in get_config(runtime=self.runtime)["modes"]}
            if not isinstance(job_input.get("mode"), str) or job_input["mode"] not in modes or "workflow" in job_input:
                raise TesterError(f"Select a named mode for the {self.runtime} runtime. Start a separate tester with the matching --runtime and worker endpoint.")
            outgoing = job_input
            if self.runtime == "dfr":
                try:
                    outgoing = dfr_module().validate_input(job_input)
                except four_k.InputError as exc:
                    raise TesterError(str(exc)) from None
            elif self.runtime == "cq-v2":
                if job_input["mode"] == "first_last_frame_cq_experimental":
                    import first_last_cq
                    try:
                        outgoing = first_last_cq.compile_workflow(job_input)
                    except four_k.InputError as exc:
                        raise TesterError(str(exc)) from None
                elif job_input["mode"] == "image_to_video_cq_experimental":
                    import i2v_cq
                    try:
                        outgoing = i2v_cq.compile_workflow(job_input)
                    except four_k.InputError as exc:
                        raise TesterError(str(exc)) from None
                else:
                    transport = os.environ.get("RUNPOD_TESTER_CQ_TRANSPORT", "named")
                    if transport not in ("workflow", "named"):
                        raise TesterError("RUNPOD_TESTER_CQ_TRANSPORT must be workflow or named.")
                    if transport == "workflow":
                        import cq_transport
                        try:
                            outgoing = cq_transport.compile_workflow(job_input)
                        except four_k.InputError as exc:
                            raise TesterError(str(exc)) from None
            elif job_input["mode"] in four_k.MODES:
                transport = os.environ.get("RUNPOD_TESTER_4K_TRANSPORT", "workflow")
                if transport not in ("workflow", "named"):
                    raise TesterError("RUNPOD_TESTER_4K_TRANSPORT must be workflow or named.")
                try:
                    if transport == "workflow":
                        outgoing = four_k.compile_workflow(job_input)
                    else:
                        four_k.prepare_input(job_input)
                except four_k.InputError as exc:
                    raise TesterError(str(exc)) from None
            envelope = {"input": outgoing}
            if self.runtime == "dfr":
                # Named DFR requests must reach the separate Python DFR image;
                # the Comfy compatibility bridge cannot implement this pipeline.
                # Include input download, verification/cropping, and delivery
                # beyond the worker's separate 60-minute pipeline limit.
                envelope["policy"] = {"executionTimeout": 5_400_000, "ttl": 7_200_000}
            elif self.runtime == "cq-v2":
                envelope["policy"] = {"executionTimeout": 3_600_000, "ttl": 7_200_000}
            elif (job_input["mode"] == "image_to_video_native_4k"
                    and job_input.get("parameters", {}).get("num_frames") == 121):
                # Give this explicit long qualification time beyond Runpod's
                # default. The deployed worker's own execution guard still applies.
                envelope["policy"] = {"executionTimeout": 3_600_000, "ttl": 7_200_000}
            payload = _json_bytes(envelope)
            if len(payload) > MAX_RUN_BYTES:
                raise TesterError("Request exceeds the tester's 9 MB limit. Use HTTPS/S3 URLs for larger media.", 413)
        req = urllib.request.Request(path, data=payload, method=method,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json", "Accept": "application/json"})
        # Never retry POST /run: a timeout can occur after Runpod accepted the job.
        try:
            with self.opener.open(req, timeout=40) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TesterError("Runpod's response exceeded 16 MiB. Use S3 output delivery.", 502,
                                  submission_uncertain=action == "run")
            try:
                result = _decode_json(raw)
            except TesterError:
                raise TesterError("Runpod returned an unreadable response.", 502,
                                  submission_uncertain=action == "run") from None
            if action == "health" and self.keep_key_in_memory:
                self.api_key = key
            return _redact_key(result, key)
        except urllib.error.HTTPError as exc:
            messages = {
                401: "Runpod rejected the API key (401). Check the key and its endpoint permissions.",
                403: "Runpod denied access (403). Check this API key's endpoint permissions.",
                404: "Runpod could not find this endpoint or job (404). Check the ID; expired jobs are removed.",
                413: "Runpod rejected the request size (413). Use HTTPS/S3 URLs for media.",
                429: "Runpod rate limit reached (429). Wait, then check the current job again.",
            }
            message = messages.get(exc.code, f"Runpod returned HTTP {exc.code}. Check endpoint status and worker logs.")
            exc.close()
            raise TesterError(message, exc.code if 400 <= exc.code <= 599 else 502,
                              submission_uncertain=action == "run" and exc.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
            raise TesterError("Runpod did not respond. Check your connection and the Runpod dashboard before submitting again.",
                              504, submission_uncertain=action == "run") from None


class TesterHandler(BaseHTTPRequestHandler):
    server_version = "LTXTester"

    def log_message(self, fmt, *args):
        # Request bodies contain credentials and media: no HTTP request logging.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(45)

    def _guard(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        if host not in allowed or len(self.headers.get_all("Host", [])) != 1:
            raise TesterError("This tester is available only through its local address.", 403)
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            raise TesterError("Cross-origin requests are not allowed.", 403)
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            raise TesterError("Open the tester directly at its local address.", 403)

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = _json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: data: blob:; media-src https: data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error):
        self._send(error.status, {"error": str(error), "submission_uncertain": error.submission_uncertain})

    def _discard_small_body(self):
        """Avoid a Windows TCP reset discarding an early HTTP error response."""
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
            return
        remaining = int(lengths[0]) - getattr(self, "_body_bytes_read", 0)
        # A rejected, same-origin binary upload can already be in flight. Drain
        # it in bounded chunks so Windows delivers the useful JSON error.
        upload = self.path == "/api/s3/upload" and getattr(self, "_guard_passed", False)
        limit = s3_upload.MAX_MEDIA_BYTES if upload else 65536
        if not 0 < remaining <= limit or int(lengths[0]) > limit:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.5 if upload else 0.25)
            deadline = time.monotonic() + (2 if upload else 0.25)
            while remaining and time.monotonic() < deadline:
                chunk = self.rfile.read(min(262144, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                self._body_bytes_read = getattr(self, "_body_bytes_read", 0) + len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def do_GET(self):
        try:
            self._guard()
            if self.path == "/api/config":
                self._send(200, get_config(self.server.endpoint_id, bool(self.server.client.api_key), self.server.runtime))
            elif self.path == "/api/sample-image":
                example = json.loads((ROOT / "examples/image_to_video_base64.json").read_text(encoding="utf-8"))
                self._send(200, {"base64": example["input"]["media"]["image"]["base64"]})
            else:
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/index.html": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if self.path not in files:
                    raise TesterError("Not found.", 404)
                filename, mime = files[self.path]
                self._send(200, (STATIC / filename).read_bytes(), mime)
        except TesterError as exc:
            self._error(exc)
        except (OSError, ValueError):
            self._error(TesterError("The local tester files could not be loaded.", 500))

    def do_POST(self):
        self._body_bytes_read = 0
        self._guard_passed = False
        try:
            self._guard()
            self._guard_passed = True
            if self.path == "/api/s3/upload":
                self._handle_s3_upload()
                return
            actions = {"/api/" + value: value for value in ("health", "run", "status", "cancel")}
            validation = self.path == "/api/s3/validate"
            if self.path not in actions and not validation:
                raise TesterError("Not found.", 404)
            if self.headers.get_content_type() != "application/json":
                raise TesterError("Content-Type must be application/json.", 415)
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                raise TesterError("A single Content-Length is required.", 400)
            length = int(lengths[0])
            if validation and not 0 < length <= 32768:
                raise TesterError("S3 settings validation must be a JSON request of at most 32 KiB.", 413)
            if not 0 < length <= MAX_BODY_BYTES:
                raise TesterError("Request exceeds 10 MB. Use HTTPS/S3 media URLs.", 413)
            raw = self.rfile.read(length)
            self._body_bytes_read = len(raw)
            if len(raw) != length:
                raise TesterError("The request upload was incomplete.")
            body = _decode_json(raw)
            if validation:
                if set(body) != {"settings", "filename", "size"}:
                    raise TesterError("S3 validation requires settings, filename, and size.")
                try:
                    result = s3_upload.validate_upload(body["settings"], body["filename"], body["size"])
                except s3_upload.S3UploadError as exc:
                    raise TesterError(str(exc), exc.status) from None
                self._send(200, result)
                return
            if set(body) - {"endpoint_id", "api_key", "job_id", "input"}:
                raise TesterError("Unsupported tester request fields.")
            result = self.server.client.request(
                actions[self.path], body.get("endpoint_id") or self.server.endpoint_id,
                api_key=body.get("api_key", ""), job_id=body.get("job_id"), job_input=body.get("input"))
            self._send(200, result)
        except TesterError as exc:
            self._discard_small_body()
            self._error(exc)
        except (TimeoutError, socket.timeout):
            self._error(TesterError("Local request upload timed out.", 408))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # Never send exception repr/tracebacks that might include request secrets.
            self._error(TesterError("The local tester encountered an error. Check the request and try checking job status.", 500))

    def _handle_s3_upload(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
            raise TesterError("A single upload Content-Length is required.")
        length = int(lengths[0])
        if not 0 < length <= s3_upload.MAX_MEDIA_BYTES:
            raise TesterError("S3 media uploads must be nonempty and at most 256 MiB.", 413)
        if len(self.headers.get_all("X-S3-Config", [])) != 1 or len(self.headers.get_all("X-Upload-Name", [])) != 1:
            raise TesterError("S3 upload settings and a filename are required.")
        try:
            settings = s3_upload.decode_settings(self.headers["X-S3-Config"])
            filename = urllib.parse.unquote(self.headers["X-Upload-Name"], errors="strict")
        except s3_upload.S3UploadError as exc:
            raise TesterError(str(exc), exc.status) from None
        except UnicodeError:
            raise TesterError("The upload filename is invalid.") from None
        if not self.server.s3_slots.acquire(blocking=False):
            raise TesterError("Two S3 uploads are already running. Wait for an upload to finish.", 429)
        handler = self

        class UploadReader:
            def read(self, size):
                data = handler.rfile.read(size)
                handler._body_bytes_read += len(data)
                return data

        try:
            result = s3_upload.upload_media(UploadReader(), length, filename, settings)
            self._send(200, result)
        except s3_upload.S3UploadError as exc:
            raise TesterError(str(exc), exc.status) from None
        finally:
            self.server.s3_slots.release()


def make_server(port=0, endpoint_id="", api_key="", keep_key_in_memory=False, runtime="comfyui"):
    endpoint = normalize_endpoint(endpoint_id) if endpoint_id else ""
    runtime = normalize_runtime(runtime)
    server = ThreadingHTTPServer(("127.0.0.1", port), TesterHandler)
    server.daemon_threads = True
    server.endpoint_id = endpoint
    server.runtime = runtime
    server.client = RunpodClient(api_key, keep_key_in_memory=keep_key_in_memory, runtime=runtime)
    server.s3_slots = threading.BoundedSemaphore(2)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--endpoint", default=os.environ.get("RUNPOD_ENDPOINT_ID", ""))
    parser.add_argument("--runtime", choices=("comfyui", "cq-v2", "dfr"), default="comfyui",
                        help="Match the endpoint's deployed image; cq-v2 and dfr are separate worker images.")
    parser.add_argument("--open", action="store_true", help="Open the local page in your browser.")
    parser.add_argument("--keep-key-in-memory", action="store_true", help="Retain a key after a successful health check until this server stops, so page reloads can reuse it.")
    args = parser.parse_args()
    try:
        server = make_server(args.port, args.endpoint, os.environ.get("RUNPOD_API_KEY", ""), args.keep_key_in_memory, args.runtime)
    except (OSError, TesterError) as exc:
        parser.exit(1, f"Cannot start tester: {exc}\nUse --port with a free local port if needed.\n")
    address = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"LTX 2.5 Runpod tester: {address}", flush=True)
    print(f"Runtime: {server.runtime}. Use the matching worker image; the tester cannot verify remote image configuration.", flush=True)
    print("Enter your Runpod API key in the page. It is kept in memory only. Ctrl+C stops this tester.", flush=True)
    print("Closing the page or this server does not cancel a Runpod job already submitted.", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
