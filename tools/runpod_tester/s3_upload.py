"""Bounded source-media uploads; SDK signs URLs locally, pinned TLS transfers bytes."""

import base64
from datetime import datetime, timedelta, timezone
import http.client
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
import urllib.parse
import uuid


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MEDIA_BYTES = 256 * 1024 * 1024
DEFAULT_EXPIRES_IN = 86400
CHUNK_BYTES = 256 * 1024


class S3UploadError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def capabilities():
    return {"available": importlib.util.find_spec("boto3") is not None,
            "max_image_bytes": MAX_IMAGE_BYTES, "max_media_bytes": MAX_MEDIA_BYTES,
            "default_expires_in": DEFAULT_EXPIRES_IN}


def _text(settings, name, default="", maximum=512):
    value = settings.get(name, default)
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise S3UploadError(f"Invalid S3 setting: {name}.")
    return value.strip()


def normalize_settings(settings):
    if not isinstance(settings, dict):
        raise S3UploadError("Enter the S3 settings before uploading.")
    allowed = {"endpoint_url", "region", "bucket", "access_key_id", "secret_access_key",
               "session_token", "prefix", "expires_in", "addressing_style"}
    if set(settings) - allowed:
        raise S3UploadError("Unsupported S3 settings.")
    endpoint = _text(settings, "endpoint_url", maximum=2048)
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or "." not in host:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise S3UploadError("Use a public HTTPS S3 service endpoint on port 443, with no bucket path, credentials, or query.") from None
    bucket = _text(settings, "bucket", maximum=63)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        raise S3UploadError("Enter the bucket name separately (3–63 lowercase letters, digits, dots or hyphens).")
    region = _text(settings, "region", "us-east-1", maximum=64)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", region):
        raise S3UploadError("Enter your S3 provider's region, such as us-east-1 or auto.")
    if host.endswith(".r2.cloudflarestorage.com") and region not in ("auto", "us-east-1"):
        raise S3UploadError("Cloudflare R2 uses the S3 region auto. A storage location such as WEUR is not an S3 signing region.")
    result = {"endpoint_url": "https://" + host, "region": region, "bucket": bucket}
    for field, limit in (("access_key_id", 256), ("secret_access_key", 512), ("session_token", 8192)):
        value = _text(settings, field, maximum=limit)
        if (field != "session_token" and not value) or not value.isascii() or re.search(r"\s", value):
            raise S3UploadError(f"Enter a valid S3 {field.replace('_', ' ')}.")
        result[field] = value
    prefix = _text(settings, "prefix", "ltx-inputs", maximum=256).strip("/")
    if prefix and (not re.fullmatch(r"[A-Za-z0-9._/-]+", prefix) or any(p in ("", ".", "..") for p in prefix.split("/"))):
        raise S3UploadError("Use a folder prefix with letters, digits, hyphens, underscores, dots and slashes.")
    result["prefix"] = prefix
    expiry = settings.get("expires_in", DEFAULT_EXPIRES_IN)
    if isinstance(expiry, bool) or not isinstance(expiry, int) or not 300 <= expiry <= 604800:
        raise S3UploadError("Signed URL expiry must be between 300 and 604800 seconds.")
    result["expires_in"] = expiry
    style = _text(settings, "addressing_style", "path", maximum=16)
    if style not in ("auto", "path", "virtual"):
        raise S3UploadError("Addressing style must be auto, path, or virtual.")
    result["addressing_style"] = style
    return result


def decode_settings(value):
    if not isinstance(value, str) or not value or len(value) > 24000:
        raise S3UploadError("S3 upload settings are missing or too large.")
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        settings = json.loads(decoded)
    except (ValueError, UnicodeError):
        raise S3UploadError("S3 upload settings could not be decoded.") from None
    return normalize_settings(settings)


def make_client(settings):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise S3UploadError("S3 uploads need boto3. Run: python -m pip install -r tools/runpod_tester/requirements.txt", 503) from None
    # No credential discovery, bucket probing, SDK retries or SDK network transfer.
    return boto3.client("s3", endpoint_url=settings["endpoint_url"], region_name=settings["region"],
                        aws_access_key_id=settings["access_key_id"],
                        aws_secret_access_key=settings["secret_access_key"],
                        aws_session_token=settings["session_token"] or None,
                        config=Config(signature_version="s3v4", proxies={},
                                      s3={"addressing_style": settings["addressing_style"]}))


def _public_addresses(host):
    try:
        addresses = list(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise S3UploadError("The S3 endpoint must resolve only to public Internet addresses.")
        return addresses
    except (socket.gaierror, ValueError):
        raise S3UploadError("The S3 endpoint hostname could not be resolved.") from None


def _validate_signed_url(url, settings):
    try:
        parsed = urllib.parse.urlsplit(url)
        host = urllib.parse.urlsplit(settings["endpoint_url"]).hostname
        allowed = {host, settings["bucket"] + "." + host}
        if (parsed.scheme != "https" or parsed.hostname not in allowed or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.fragment):
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise S3UploadError("The signed URL does not match the configured S3 endpoint.") from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw_socket = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


def _open_connection(url, settings):
    parsed = _validate_signed_url(url, settings)
    addresses = _public_addresses(parsed.hostname)
    return PinnedHTTPSConnection(parsed.hostname, addresses[0]), parsed.path + ("?" + parsed.query if parsed.query else "")


def _http_error(status, operation):
    if status in (301, 302, 303, 307, 308):
        return S3UploadError("S3 requested a redirect. Enter the correct service endpoint and region; redirects are not followed.", 502)
    if status in (401, 403):
        return S3UploadError(f"S3 denied {operation} (HTTP {status}). Check credentials, region, and PutObject/GetObject permissions.", 403)
    if status == 404:
        return S3UploadError("S3 could not find the bucket or uploaded object. Check the bucket and endpoint.", 404)
    return S3UploadError(f"S3 {operation} failed (HTTP {status}). Check the bucket, region, and storage settings.", 502)


def _put_file(url, settings, path, size, content_type):
    connection, target = _open_connection(url, settings)
    try:
        connection.putrequest("PUT", target, skip_accept_encoding=True)
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(size))
        connection.endheaders()
        deadline = time.monotonic() + 540
        with open(path, "rb") as source:
            while chunk := source.read(CHUNK_BYTES):
                if time.monotonic() > deadline:
                    raise S3UploadError("The S3 upload exceeded nine minutes. Check the bucket before uploading again.", 504)
                connection.send(chunk)
        response = connection.getresponse()
        if response.status not in (200, 201, 204):
            raise _http_error(response.status, "upload")
        response.read(8192)
    finally:
        connection.close()


def _verify_url(url, settings, size, prefix):
    connection, target = _open_connection(url, settings)
    try:
        connection.request("GET", target, headers={"Range": f"bytes=0-{len(prefix) - 1}"})
        response = connection.getresponse()
        if response.status not in (200, 206):
            raise _http_error(response.status, "read verification")
        if response.status == 206:
            expected = f"bytes 0-{len(prefix) - 1}/{size}"
            correct_size = response.getheader("Content-Range") == expected
        else:
            correct_size = response.getheader("Content-Length") == str(size)
        if not correct_size or response.read(len(prefix)) != prefix:
            raise S3UploadError("The uploaded object's signed URL returned unexpected size or bytes.", 502)
    finally:
        connection.close()


TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
}


def _media_info(filename, size):
    if (not isinstance(filename, str) or not filename or len(filename) > 255
            or "/" in filename or "\\" in filename or any(ord(char) < 32 for char in filename)):
        raise S3UploadError("Use a valid media filename without a folder path.")
    extension = Path(filename).suffix.lower()
    content_type = TYPES.get(extension)
    if not content_type:
        raise S3UploadError("Choose a supported image, video, or audio file.")
    limit = MAX_IMAGE_BYTES if content_type.startswith("image/") else MAX_MEDIA_BYTES
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= limit:
        raise S3UploadError(f"The file must be nonempty and no larger than {limit // 1024 // 1024} MiB.", 413)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:100].strip("._") or "media"
    return safe_name + extension, extension, content_type


def validate_upload(settings, filename, size):
    """Validate locally before sending file bytes; never contact storage."""
    normalize_settings(settings)
    _media_info(filename, size)
    if not capabilities()["available"]:
        raise S3UploadError("S3 uploads need boto3. Run: python -m pip install -r tools/runpod_tester/requirements.txt", 503)
    return {"valid": True}


def _check_magic(prefix, extension):
    allowed = {
        ".png": prefix.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": prefix.startswith(b"\xff\xd8\xff"), ".jpeg": prefix.startswith(b"\xff\xd8\xff"),
        ".webp": prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP",
        ".bmp": prefix.startswith(b"BM"),
        ".tif": prefix.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")),
        ".tiff": prefix.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")),
        ".mp4": prefix[4:8] == b"ftyp", ".mov": prefix[4:8] == b"ftyp", ".m4a": prefix[4:8] == b"ftyp",
        ".webm": prefix.startswith(b"\x1aE\xdf\xa3"), ".mkv": prefix.startswith(b"\x1aE\xdf\xa3"),
        ".avi": prefix.startswith(b"RIFF") and prefix[8:12] == b"AVI ",
        ".wav": prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE",
        ".mp3": prefix.startswith(b"ID3") or len(prefix) >= 2 and prefix[0] == 255 and prefix[1] & 224 == 224,
        ".flac": prefix.startswith(b"fLaC"), ".ogg": prefix.startswith(b"OggS"),
        ".oga": prefix.startswith(b"OggS"), ".opus": prefix.startswith(b"OggS"),
    }
    if not allowed.get(extension):
        raise S3UploadError("The file contents do not match a supported media container. Select a valid media file.")


def upload_media(stream, content_length, filename, settings):
    settings = normalize_settings(settings)
    safe_name, extension, content_type = _media_info(filename, content_length)
    client = None
    temporary = None
    uploaded = False
    try:
        client = make_client(settings)
        prefix = stream.read(min(64, content_length))
        if len(prefix) != min(64, content_length):
            raise S3UploadError("The local media upload was incomplete.")
        _check_magic(prefix, extension)
        with tempfile.NamedTemporaryFile(prefix="ltx-s3-", suffix=extension, delete=False) as destination:
            temporary = destination.name
            destination.write(prefix)
            remaining = content_length - len(prefix)
            deadline = time.monotonic() + 300
            while remaining:
                if time.monotonic() > deadline:
                    raise S3UploadError("The local media upload timed out.", 408)
                chunk = stream.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise S3UploadError("The local media upload was incomplete.")
                destination.write(chunk)
                remaining -= len(chunk)
        object_name = uuid.uuid4().hex + "-" + safe_name
        key = settings["prefix"] + "/" + object_name if settings["prefix"] else object_name
        params = {"Bucket": settings["bucket"], "Key": key}
        put_url = client.generate_presigned_url("put_object", Params=dict(params, ContentType=content_type),
                                                ExpiresIn=900, HttpMethod="PUT")
        _put_file(put_url, settings, temporary, content_length, content_type)
        uploaded = True
        signed_at = datetime.now(timezone.utc)
        url = client.generate_presigned_url("get_object", Params=params,
                                            ExpiresIn=settings["expires_in"], HttpMethod="GET")
        _verify_url(url, settings, content_length, prefix)
        expires_at = signed_at + timedelta(seconds=settings["expires_in"])
        return {"url": url, "bucket": settings["bucket"], "key": key, "filename": safe_name,
                "size": content_length, "content_type": content_type, "expires_in": settings["expires_in"],
                "expires_at": expires_at.isoformat(), "verified": True}
    except S3UploadError as exc:
        if uploaded:
            raise S3UploadError(str(exc) + " The object was uploaded and has been retained in your bucket.", exc.status) from None
        raise
    except Exception:
        suffix = " The uploaded object has been retained in your bucket." if uploaded else " Check the bucket before uploading again if the connection failed."
        # SDK/HTTP exceptions may contain a URL, access key or signature: never echo them.
        raise S3UploadError("S3 upload or URL verification failed. Check your endpoint, region, credentials and network connection." + suffix, 502) from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                raise S3UploadError("The temporary upload file could not be removed. Check the local temporary folder; any S3 object is retained.", 500) from None
