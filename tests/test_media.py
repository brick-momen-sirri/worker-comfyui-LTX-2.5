import base64
from io import BytesIO
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from ltx_worker.errors import InputError
from ltx_worker.media import _resolve_url, download, ingest


def encoded_image():
    stream = BytesIO()
    Image.new("RGB", (40, 24), "red").save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode()


class FakeResponse:
    def __init__(self, status=200, blocks=(), headers=None):
        self.status = status
        self.blocks = iter(blocks)
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, _):
        return next(self.blocks, b"")


class FakeConnection:
    response = None

    def __init__(self, *args, **kwargs):
        pass

    def request(self, *args, **kwargs):
        pass

    def getresponse(self):
        return self.response

    def close(self):
        pass


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_raw_and_data_uri_images_normalize_to_png(self):
        for i, value in enumerate((encoded_image(), "data:image/png;base64," + encoded_image(), {"base64": encoded_image()})):
            output = ingest(value, "image", self.root / str(i), "image", {})
            with Image.open(output) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (40, 24))
            self.assertFalse(output.with_suffix(".source").exists())

    def test_url_image_uses_identical_validation(self):
        def fetch(url, destination, limit):
            destination.write_bytes(base64.b64decode(encoded_image()))
        with patch("ltx_worker.media.download", side_effect=fetch) as mock:
            output = ingest({"url": "https://bucket.s3.amazonaws.com/a?X-Amz-Signature=secret"}, "image", self.root, "first_frame", {})
            self.assertTrue(output.exists())
            mock.assert_called_once()

    def test_rejects_bad_base64_mime_empty_ambiguous_or_nonimage(self):
        for value in ("invalid!!", "", "data:video/mp4;base64," + encoded_image(), {"url": "https://a", "base64": encoded_image()}, base64.b64encode(b"not an image").decode()):
            with self.subTest(value=type(value)), self.assertRaises(InputError):
                ingest(value, "image", self.root, "image", {})

    def test_media_size_checked_before_decode(self):
        with patch.dict("os.environ", {"MAX_IMAGE_BYTES": "8"}), self.assertRaises(InputError) as error:
            ingest(encoded_image(), "image", self.root, "image", {})
        self.assertEqual(error.exception.code, "MEDIA_TOO_LARGE")

    def test_private_mixed_dns_redirect_and_http_rejected(self):
        for url in ("http://example.org/a", "https://user:pass@example.org/a", "https://example.org:8443/a", "https://example.org/a\r\nHeader: bad"):
            with self.assertRaises(InputError):
                _resolve_url(url)
        for addresses in (("127.0.0.1",), ("169.254.169.254",), ("93.184.216.34", "10.0.0.1"), ("::1",)):
            resolved = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]
            with patch("socket.getaddrinfo", return_value=resolved), self.assertRaises(InputError):
                _resolve_url("https://bucket.s3.amazonaws.com/private")
        FakeConnection.response = FakeResponse(302, headers={"Location": "http://127.0.0.1/"})
        with patch("ltx_worker.media._PinnedHTTPSConnection", FakeConnection), patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]), self.assertRaises(InputError):
            download("https://public.example/a", self.root / "download", 10)

    def test_stream_download_limits_truncation_and_redacted_error(self):
        with patch("ltx_worker.media._resolve_url", return_value=(__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit("https://bucket.s3.amazonaws.com/a?secret"), "bucket.s3.amazonaws.com", "93.184.216.34")), patch("ltx_worker.media._PinnedHTTPSConnection", FakeConnection):
            FakeConnection.response = FakeResponse(blocks=(b"1234", b"5678"))
            with self.assertRaises(InputError):
                download("https://bucket.s3.amazonaws.com/a?secret", self.root / "a", 6)
            FakeConnection.response = FakeResponse(blocks=(b"123",), headers={"Content-Length": "5"})
            with self.assertRaises(InputError):
                download("https://bucket.s3.amazonaws.com/a?secret", self.root / "b", 10)
            FakeConnection.response = FakeResponse(403)
            with self.assertRaises(InputError) as error:
                download("https://bucket.s3.amazonaws.com/a?secret", self.root / "c", 10)
            self.assertNotIn("secret", str(error.exception))
            self.assertIn("403", str(error.exception))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe are unavailable")
    def test_actual_video_audio_normalization_and_short_input_errors(self):
        source = self.root / "fixture.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=24:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)], check=True, capture_output=True)
        encoded = "data:video/mp4;base64," + base64.b64encode(source.read_bytes()).decode()
        params = {"width": 64, "height": 64, "fps": 24, "num_frames": 9}
        result = ingest(encoded, "video", self.root / "video", "video", params, require_audio=True)
        info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result)]))
        self.assertEqual(int(info["streams"][0]["nb_frames"]), 9)
        self.assertEqual(info["streams"][0]["width"], 64)
        self.assertIn("audio", [s["codec_type"] for s in info["streams"]])
        with self.assertRaises(InputError) as error:
            ingest(encoded, "video", self.root / "short", "video", dict(params, num_frames=49))
        self.assertIn("too short", str(error.exception))
        audio = self.root / "fixture.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-vn", str(audio)], check=True, capture_output=True)
        audio_result = ingest({"base64": base64.b64encode(audio.read_bytes()).decode()}, "audio", self.root / "audio", "audio", params)
        self.assertGreater(audio_result.stat().st_size, 44)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe are unavailable")
    def test_upscale_normalization_adds_silence_when_audio_is_absent(self):
        source = self.root / "silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=64x64:r=24:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)], check=True, capture_output=True)
        encoded = base64.b64encode(source.read_bytes()).decode()
        params = {"width": 64, "height": 64, "fps": 24, "num_frames": 9}
        result = ingest(encoded, "video", self.root / "upscale", "video", params, ensure_audio=True)
        info = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result)]))
        self.assertEqual(int(info["streams"][0]["nb_frames"]), 9)
        self.assertIn("audio", [s["codec_type"] for s in info["streams"]])
        with self.assertRaises(InputError) as error:
            ingest(encoded, "video", self.root / "requires_audio", "video", params, require_audio=True)
        self.assertIn("audio stream", str(error.exception))


if __name__ == "__main__":
    unittest.main()
