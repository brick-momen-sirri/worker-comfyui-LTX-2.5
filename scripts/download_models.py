"""Build-only immutable Hugging Face downloads, without an HF cache or token file."""

from __future__ import annotations

import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DownloadError(RuntimeError):
    pass


class DownloadCancelled(DownloadError):
    """An unfinished transfer stopped because another bundle file failed."""


def _check_cancelled(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise DownloadCancelled("Model download cancelled after another file failed")


class SafeRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise DownloadError("Model server attempted a non-HTTPS redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class NoAccessRedirects(HTTPRedirectHandler):
    """Inspect the Hub response without following a signed CDN download URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def model_request(entry: dict, token: str, method: str = "GET") -> Request:
    url = (
        f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/"
        f"{quote(entry['filename'], safe='/')}?download=true"
    )
    headers = {"User-Agent": "worker-comfyui-ltx25/1", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Request(url, headers=headers, method=method)


def valid_access_redirect(request: Request, location: str | None) -> bool:
    """Never expose or request the redirect target, including its signature."""
    if not location:
        return False
    try:
        target = urlsplit(urljoin(request.full_url, location))
        return bool(target.scheme == "https" and target.hostname
                    and target.username is None and target.password is None
                    and target.port in (None, 443))
    except ValueError:
        return False


def probe_access(entry: dict, token: str, attempts: int = 3) -> tuple[str, str] | None:
    """Return a sanitized problem category/reason, or None when Hub access works."""
    opener = build_opener(NoAccessRedirects())
    request = model_request(entry, token, method="HEAD")
    reason = "No attempts made"
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(request, timeout=30) as response:
                status, location = response.status, response.headers.get("Location")
        except HTTPError as error:
            status, location = error.code, error.headers.get("Location") if error.headers else None
            error.close()
        except (OSError, URLError) as error:
            # Exception messages may contain request URLs or credentials.
            status, location = None, None
            reason = type(error).__name__
        if status == 200:
            return None
        if status in (301, 302, 303, 307, 308):
            if valid_access_redirect(request, location):
                return None
            return "failed", "Hub returned an invalid or non-HTTPS redirect"
        if status in (401, 403):
            return "denied", f"HTTP {status}"
        if status == 404:
            return "missing", "HTTP 404"
        if status is not None:
            reason = f"HTTP {status}"
            if status not in (408, 425, 429) and not 500 <= status <= 599:
                return "failed", reason
        if attempt < attempts:
            time.sleep(min(attempt * 5, 15))
    return "failed", reason


def check_access(entries: list[dict], root: Path, token: str, attempts: int = 3) -> list[dict]:
    """Check every uncached file before downloading any; report all access issues."""
    pending, denied, missing, failed = [], set(), [], []
    for entry in entries:
        if verify_file(model_path(root, entry), entry):
            print(f"Verified cached model: {entry['destination']}", flush=True)
            continue
        pending.append(entry)
        problem = probe_access(entry, token, attempts=attempts)
        if problem is None:
            continue
        category, reason = problem
        if category == "denied":
            denied.add(entry["repo"])
        elif category == "missing":
            missing.append(f"{entry['repo']}/{entry['filename']}")
        else:
            failed.append(f"{entry['destination']} ({reason})")
    errors = []
    if denied:
        errors.append("Access denied for repositories: " + ", ".join(sorted(denied))
                      + ". Accept each repository's Hugging Face access terms and grant the "
                      "read token access; supply it through the hf_token BuildKit secret")
    if missing:
        errors.append("Pinned files not found: " + ", ".join(missing))
    if failed:
        errors.append("Other access checks failed: " + ", ".join(failed))
    if errors:
        raise DownloadError("Model access preflight failed. " + "; ".join(errors)
                            + ". No models were downloaded")
    print(f"Model access preflight passed: {len(pending)} uncached files", flush=True)
    return pending


def load_manifest(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not data.get("models"):
        raise DownloadError("Unsupported or empty model manifest")
    seen = set()
    for entry in data["models"]:
        for key in ("filename", "destination"):
            relative = PurePosixPath(entry[key])
            if relative.is_absolute() or ".." in relative.parts or "\\" in entry[key]:
                raise DownloadError(f"Unsafe manifest {key}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repo"]):
            raise DownloadError("Invalid Hugging Face repository")
        if not re.fullmatch(r"[0-9a-f]{40}", entry["revision"]):
            raise DownloadError("Model revision must be an immutable Git commit")
        if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise DownloadError("Model SHA256 is missing or invalid")
        if not isinstance(entry["size"], int) or entry["size"] <= 0:
            raise DownloadError("Model size must be a positive integer")
        if entry["destination"] in seen:
            raise DownloadError("Duplicate model destination")
        seen.add(entry["destination"])
    return data["models"]


def model_path(root: Path, entry: dict) -> Path:
    root = root.resolve()
    result = (root / entry["destination"]).resolve()
    if result == root or not result.is_relative_to(root):
        raise DownloadError("Model destination escapes model root")
    return result


def validate_destinations(entries: list[dict], root: Path) -> None:
    """Claim each final/partial path once, including resolved aliases and parents."""
    claimed = set()
    resolved_root = root.resolve()
    for entry in entries:
        target = model_path(root, entry)
        for path in (target, target.with_name(target.name + ".partial").resolve()):
            if not path.is_relative_to(resolved_root) or path == resolved_root:
                raise DownloadError("Model partial-file path escapes model root")
            if path in claimed:
                raise DownloadError("Conflicting model destination or partial-file path")
            claimed.add(path)
    if any(parent in claimed for path in claimed for parent in path.parents):
        raise DownloadError("A model destination conflicts with another model directory")


def verify_file(path: Path, entry: dict, full_hash: bool = True) -> bool:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    if not full_hash:
        return True
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == entry["sha256"]


def download(entry: dict, root: Path, token: str, attempts: int = 3,
             stop_event=None) -> None:
    _check_cancelled(stop_event)
    target = model_path(root, entry)
    if verify_file(target, entry):
        print(f"Verified cached model: {entry['destination']}", flush=True)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".partial")
    request = model_request(entry, token)
    opener = build_opener(SafeRedirects())
    for attempt in range(1, attempts + 1):
        try:
            _check_cancelled(stop_event)
            print(f"Downloading {entry['destination']} ({entry['size']} bytes), attempt {attempt}", flush=True)
            digest = hashlib.sha256()
            count = 0
            last_progress = time.monotonic()
            with opener.open(request, timeout=120) as response:
                if response.status != 200:
                    raise DownloadError("Model server returned a non-200 response")
                with temporary.open("wb") as output:
                    while True:
                        _check_cancelled(stop_event)
                        chunk = response.read(8 * 1024 * 1024)
                        _check_cancelled(stop_event)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > entry["size"]:
                            raise DownloadError("Downloaded model exceeds manifest size")
                        output.write(chunk)
                        digest.update(chunk)
                        now = time.monotonic()
                        if now - last_progress >= 30:
                            print(f"Download progress: {entry['destination']} "
                                  f"{count}/{entry['size']} bytes", flush=True)
                            last_progress = now
                    output.flush()
                    os.fsync(output.fileno())
            if count != entry["size"] or digest.hexdigest() != entry["sha256"]:
                raise DownloadError("Downloaded model failed size/SHA256 verification")
            _check_cancelled(stop_event)
            temporary.replace(target)
            print(f"Verified model: {entry['destination']}", flush=True)
            return
        except DownloadCancelled:
            raise
        except HTTPError as error:
            # Do not serialize request objects, signed CDN URLs, or credentials.
            error.close()
            if error.code in (401, 403):
                raise DownloadError(
                    f"Access denied for {entry['repo']}; accept its Hugging Face access terms "
                    "and provide a read token through the hf_token BuildKit secret"
                ) from None
            if error.code == 404:
                raise DownloadError(f"Pinned file not found: {entry['destination']}") from None
            reason = f"HTTP {error.code}"
        except (OSError, URLError, DownloadError) as error:
            reason = type(error).__name__
        finally:
            temporary.unlink(missing_ok=True)
        if attempt == attempts:
            raise DownloadError(f"Failed downloading {entry['destination']}: {reason}")
        _check_cancelled(stop_event)
        delay = min(attempt * 5, 15)
        if stop_event is None:
            time.sleep(delay)
        elif stop_event.wait(delay):
            _check_cancelled(stop_event)


def download_all(entries: list[dict], root: Path, token: str, workers: int = 1) -> None:
    """Download independent files; preserve the original sequential call path."""
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise DownloadError("Download workers must be an integer from 1 to 4")
    validate_destinations(entries, root)
    if workers == 1:
        for entry in entries:
            download(entry, root, token)
        return
    stop_event = threading.Event()
    failure_lock = threading.Lock()
    failures = []

    def transfer(entry):
        _check_cancelled(stop_event)
        try:
            download(entry, root, token, stop_event=stop_event)
        except DownloadCancelled:
            raise
        except Exception as error:
            with failure_lock:
                if not failures:
                    failures.append((entry["destination"], type(error).__name__))
                stop_event.set()
            raise

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="model-download")
    futures = []
    try:
        for entry in entries:
            if stop_event.is_set():
                break
            futures.append(executor.submit(transfer, entry))
        for future in as_completed(futures):
            try:
                future.result()
            except (CancelledError, DownloadCancelled):
                continue
            except Exception:
                stop_event.set()
                for pending in futures:
                    pending.cancel()
                break
    except BaseException:
        stop_event.set()
        for pending in futures:
            pending.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=stop_event.is_set())
    if failures:
        destination, reason = failures[0]
        # Unexpected exceptions can embed signed URLs or request credentials.
        raise DownloadError(f"Parallel download failed for {destination}: {reason}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/models-manifest.json"))
    parser.add_argument("--root", type=Path, default=Path("/comfyui/models"))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=1,
                        help="Independent model downloads in parallel (1-4; default: 1)")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify-only", action="store_true")
    action.add_argument("--check-access-only", action="store_true",
                        help="Check every uncached pinned Hub file without downloading models")
    parser.add_argument("--size-only", action="store_true", help="Fast startup check; hashes always checked during build")
    args = parser.parse_args()
    try:
        entries = load_manifest(args.manifest)
        validate_destinations(entries, args.root)
        if args.verify_only:
            missing = [entry["destination"] for entry in entries if not verify_file(
                model_path(args.root, entry), entry, full_hash=not args.size_only
            )]
            if missing:
                raise DownloadError("Missing or invalid baked models: " + ", ".join(missing))
            print(f"Verified {len(entries)} model files ({sum(x['size'] for x in entries)} bytes)")
            return
        token = args.token_file.read_text().strip() if args.token_file and args.token_file.is_file() else ""
        pending = check_access(entries, args.root, token)
        if args.check_access_only or not pending:
            return
        args.root.mkdir(parents=True, exist_ok=True)
        needed = sum(entry["size"] for entry in pending)
        if shutil.disk_usage(args.root).free < needed + 1024**3:
            raise DownloadError(f"Insufficient free space: at least {needed + 1024**3} bytes required")
        download_all(pending, args.root, token, workers=args.workers)
    except (DownloadError, ValueError) as error:
        parser.exit(1, f"Model bundle error: {error}\n")


if __name__ == "__main__":
    main()
