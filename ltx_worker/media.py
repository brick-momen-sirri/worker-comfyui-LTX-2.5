"""Bounded media ingestion. Download URLs are never logged or echoed in errors."""

import base64
import binascii
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time
from urllib.parse import urljoin, urlsplit
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import InputError


def _limit(name, default):
    return int(os.environ.get(name, default))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a vetted address while preserving TLS SNI and certificate checks."""

    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, port=443, timeout=timeout,
                         context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _resolve_url(url):
    if not isinstance(url, str) or len(url) > 16384 or re.search(r"[\x00-\x20\x7f]", url):
        raise InputError("Media URL is malformed", "INVALID_URL")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        raise InputError("Media URLs must use HTTPS on port 443 without credentials or fragments", "INVALID_URL") from None
    allowlist = [h.strip().lower() for h in os.environ.get("INPUT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if allowlist and host.lower() not in allowlist:
        raise InputError("Media URL host is not allowed", "INVALID_URL")
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise InputError("Media URLs must resolve only to public IP addresses", "INVALID_URL")
    except OSError:
        raise InputError("Could not resolve media URL host", "DOWNLOAD_FAILED") from None
    return parsed, host, addresses[0]


def download(url, destination, max_bytes):
    deadline = time.monotonic() + _limit("INPUT_DOWNLOAD_TIMEOUT_S", 300)
    for redirect in range(4):
        if time.monotonic() >= deadline:
            raise InputError("Media download exceeded its time limit", "DOWNLOAD_FAILED")
        parsed, host, address = _resolve_url(url)
        connection = _PinnedHTTPSConnection(host, address, timeout=15)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"Host": host, "Accept-Encoding": "identity", "User-Agent": "LTX25-Runpod-Worker/1"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise InputError("Media URL exceeded redirect limit", "DOWNLOAD_FAILED")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise InputError(f"Media download returned HTTP {response.status}; check object access and URL expiry", "DOWNLOAD_FAILED")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise InputError("Compressed HTTP media responses are unsupported", "DOWNLOAD_FAILED")
            content_length = response.getheader("Content-Length")
            try:
                expected = int(content_length) if content_length else None
                if expected is not None and (expected < 0 or expected > max_bytes):
                    raise InputError("Media exceeds the byte limit", "MEDIA_TOO_LARGE")
            except ValueError:
                raise InputError("Invalid media response length", "DOWNLOAD_FAILED") from None
            written = 0
            with open(destination, "wb") as output:
                while True:
                    if time.monotonic() >= deadline:
                        raise InputError("Media download exceeded its time limit", "DOWNLOAD_FAILED")
                    # read1 returns available data, preventing trickle feeds from resetting
                    # a socket timeout indefinitely inside a full-size read().
                    block = response.read1(64 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > max_bytes:
                        raise InputError("Media exceeds the byte limit", "MEDIA_TOO_LARGE")
                    output.write(block)
            if written == 0 or (expected is not None and written != expected):
                raise InputError("Media download was empty or incomplete", "DOWNLOAD_FAILED")
            return
        except (OSError, http.client.HTTPException, UnicodeError):
            raise InputError("Media download failed or timed out; check object access and URL expiry", "DOWNLOAD_FAILED") from None
        finally:
            connection.close()


def _write_source(source, path, max_bytes, kind):
    if isinstance(source, dict):
        if len(source) != 1 or next(iter(source), "") not in ("base64", "url"):
            raise InputError("Each media object must contain exactly one of 'base64' or 'url'")
        key, value = next(iter(source.items()))
    elif isinstance(source, str):
        key, value = ("url" if source.startswith(("https://", "http://")) else "base64"), source
    else:
        raise InputError("Media must be a base64 string, HTTPS URL, or a base64/url object")
    if not isinstance(value, str) or not value:
        raise InputError("Media source must be a nonempty string")
    if key == "url":
        download(value, path, max_bytes)
        return
    if value.startswith("data:"):
        header, separator, value = value.partition(",")
        if not separator or not re.fullmatch(r"data:(image|video|audio)/[A-Za-z0-9.+-]+;base64", header):
            raise InputError("Media data URI must declare a media MIME type and base64 encoding")
        if not header.startswith("data:" + kind + "/"):
            raise InputError(f"Expected a {kind} data URI")
    if len(value) > ((max_bytes + 2) // 3) * 4:
        raise InputError("Media exceeds the byte limit", "MEDIA_TOO_LARGE")
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise InputError("Media contains invalid base64", "INVALID_MEDIA") from None
    if not raw or len(raw) > max_bytes:
        raise InputError("Media is empty or exceeds the byte limit", "MEDIA_TOO_LARGE")
    path.write_bytes(raw)


def _normalize_image(source, destination):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as probe:
                if getattr(probe, "n_frames", 1) != 1:
                    raise InputError("Image inputs must be still images", "INVALID_MEDIA")
                if probe.width * probe.height > _limit("MAX_IMAGE_PIXELS", 40_000_000):
                    raise InputError("Image exceeds the pixel limit", "MEDIA_TOO_LARGE")
                if probe.format not in {"PNG", "JPEG", "WEBP", "BMP", "TIFF"}:
                    raise InputError("Unsupported image format; use PNG, JPEG, WebP, BMP, or TIFF", "INVALID_MEDIA")
                probe.verify()
            with Image.open(source) as im:
                ImageOps.exif_transpose(im).convert("RGB").save(destination, "PNG")
    except InputError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise InputError("Image is corrupt or exceeds safe decoder limits", "INVALID_MEDIA") from None


def _run_media(command):
    try:
        return subprocess.run(command, capture_output=True, check=True,
                              timeout=_limit("MEDIA_PROCESS_TIMEOUT_S", 180)).stdout
    except FileNotFoundError:
        raise InputError("Worker media tools are missing; rebuild the image", "WORKER_CONFIGURATION") from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise InputError("Media could not be decoded within the processing limit", "INVALID_MEDIA") from None


def _normalize_av(source, destination, kind, params, require_audio=False, ensure_audio=False):
    # Container signatures prevent ffmpeg from accepting playlists, device inputs,
    # or text demuxers. Nested network protocols are disabled as an extra boundary.
    with source.open("rb") as f:
        head = f.read(16)
    valid = (head[4:8] == b"ftyp" or head.startswith((b"\x1aE\xdf\xa3", b"RIFF", b"OggS", b"fLaC", b"ID3"))
             or (len(head) > 1 and head[0] == 255 and head[1] & 0xE0 == 0xE0))
    if not valid:
        raise InputError("Unsupported media container", "INVALID_MEDIA")
    payload = _run_media(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(source)])
    try:
        info = json.loads(payload)
        streams = info["streams"]
        stream = next(s for s in streams if s.get("codec_type") == kind)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if require_audio and not has_audio:
            raise InputError("This workflow requires a video containing an audio stream", "INVALID_MEDIA")
        duration = float(info.get("format", {}).get("duration", stream.get("duration", 0)))
        if not math.isfinite(duration) or duration <= 0 or duration > _limit("MAX_INPUT_DURATION_S", 120):
            raise InputError("Media duration must be known and within MAX_INPUT_DURATION_S", "INVALID_MEDIA")
        if kind == "video" and (int(stream["width"]) * int(stream["height"]) > _limit("MAX_VIDEO_PIXELS", 8_847_360)):
            raise InputError("Video resolution exceeds the pixel limit", "MEDIA_TOO_LARGE")
    except InputError:
        raise
    except (KeyError, ValueError, TypeError, StopIteration):
        raise InputError(f"Input must contain a valid {kind} stream with a known duration", "INVALID_MEDIA") from None
    command = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file,pipe", "-threads", "2", "-i", str(source)]
    if kind == "video":
        if ensure_audio and not has_audio:
            command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        fps = params.get("fps", 24)
        width, height = params.get("width", 768), params.get("height", 512)
        filters = f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
        command += ["-map", "0:v:0", "-map", "1:a:0" if ensure_audio and not has_audio else "0:a:0?", "-vf", filters, "-frames:v", str(params.get("num_frames", 121)),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", "-movflags", "+faststart"]
        command += ["-t", str(params.get("num_frames", 121) / fps)]
    else:
        required_duration = params.get("num_frames", 121) / params.get("fps", 24)
        if duration + 0.01 < required_duration:
            raise InputError("Audio is shorter than num_frames / fps", "INVALID_MEDIA")
        command += ["-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", "-t", str(required_duration)]
    command += ["-map_metadata", "-1", "-threads", "2", str(destination)]
    _run_media(command)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise InputError("Media normalization produced no data", "INVALID_MEDIA")
    if kind == "video":
        try:
            actual = json.loads(_run_media(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames", "-of", "json", str(destination)]))
            frame_count = int(actual["streams"][0].get("nb_frames", 0))
        except (ValueError, KeyError, TypeError, IndexError):
            raise InputError("Could not validate the normalized video's frame count", "INVALID_MEDIA") from None
        if frame_count < params.get("num_frames", 121):
            raise InputError("Video is too short for num_frames at the requested fps", "INVALID_MEDIA")
    return {"duration": duration}


def ingest(source, kind, directory, role, params, require_audio=False, ensure_audio=False):
    if kind not in {"image", "video", "audio"} or not re.fullmatch(r"[a-z][a-z0-9_]*", role):
        raise InputError("Worker media binding is invalid", "WORKER_CONFIGURATION")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / (role + ".source")
    destination = directory / (role + {"image": ".png", "video": ".mp4", "audio": ".wav"}[kind])
    limit = _limit("MAX_IMAGE_BYTES" if kind == "image" else "MAX_MEDIA_BYTES", 20 * 1024**2 if kind == "image" else 256 * 1024**2)
    try:
        _write_source(source, raw, limit, kind)
        if kind == "image":
            _normalize_image(raw, destination)
        else:
            _normalize_av(raw, destination, kind, params, require_audio, ensure_audio)
        return destination
    finally:
        raw.unlink(missing_ok=True)
