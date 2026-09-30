"""S3 uploader tests: no AWS requests, credentials, buckets, or paid jobs.

Run with: python -m unittest discover -s tools/runpod_tester -p test_s3.py -v
"""

import base64
import contextlib
from datetime import datetime, timezone
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.parse


MODULE_PATH = Path(__file__).with_name("s3_upload.py")
SPEC = importlib.util.spec_from_file_location("s3_uploader_under_test", MODULE_PATH)
s3 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s3)
ROOT = MODULE_PATH.parents[2]
PNG = base64.b64decode(json.loads(
    (ROOT / "examples/image_to_video_base64.json").read_text()
)["input"]["media"]["image"]["base64"])


def settings(**changes):
    result = {
        "endpoint_url": "https://s3.example.com", "bucket": "test-media",
        "access_key_id": "test-access-key", "secret_access_key": "test-secret-key",
    }
    result.update(changes)
    return result


def address_result(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (value, 443))
            for value in addresses]


class SettingsTests(unittest.TestCase):
    def test_r2_requires_an_s3_signing_region_not_a_storage_location(self):
        endpoint = "https://account.r2.cloudflarestorage.com"
        for region in ("auto", "us-east-1"):
            with self.subTest(region=region):
                actual = s3.normalize_settings(settings(endpoint_url=endpoint, region=region))
                self.assertEqual(actual["region"], region)
        with self.assertRaises(s3.S3UploadError) as raised:
            s3.normalize_settings(settings(endpoint_url=endpoint, region="WEUR"))
        self.assertIn("auto", str(raised.exception))
        self.assertIn("signing region", str(raised.exception))

    def test_defaults_and_normalized_settings_round_trip(self):
        normalized = s3.normalize_settings(settings(endpoint_url="https://S3.EXAMPLE.COM:443/"))
        self.assertEqual(normalized["endpoint_url"], "https://s3.example.com")
        self.assertEqual(normalized["region"], "us-east-1")
        self.assertEqual(normalized["expires_in"], 86400)
        self.assertEqual(normalized["session_token"], "")
        self.assertEqual(s3.normalize_settings(normalized), normalized)
        for style in ("auto", "path", "virtual"):
            with self.subTest(style=style):
                self.assertEqual(s3.normalize_settings(settings(addressing_style=style))
                                 ["addressing_style"], style)

    def test_unsafe_endpoints_and_embedded_credentials_are_rejected(self):
        secret = "do-not-print-me"
        for endpoint in (
            "", "http://s3.example.com", "https://localhost", "https://127.0.0.1",
            "https://169.254.169.254", "https://10.0.0.1", "https://[::1]",
            "https://s3.example.com:444", "https://s3.example.com/bucket",
            "https://user:" + secret + "@s3.example.com",
            "https://s3.example.com?key=" + secret, "https://s3.example.com#" + secret,
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(s3.S3UploadError) as raised:
                    s3.normalize_settings(settings(endpoint_url=endpoint))
                self.assertNotIn(secret, str(raised.exception))

    def test_invalid_settings_fail_without_echoing_values(self):
        cases = [None, [], {}, settings(unknown="secret"), settings(bucket="AB"),
                 settings(bucket="bad/name"), settings(bucket="a..b"),
                 settings(region=""), settings(region="bad/region"),
                 settings(access_key_id=""), settings(secret_access_key=""),
                 settings(secret_access_key="has space"), settings(session_token=42),
                 settings(secret_access_key="unicode-é"), settings(prefix="../escape"),
                 settings(prefix="a//b"), settings(prefix="a\\b"),
                 settings(addressing_style="unsupported")]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(s3.S3UploadError):
                s3.normalize_settings(value)
        for expiry in (299, 604801, True, "86400", 300.0, None):
            with self.subTest(expiry=expiry), self.assertRaises(s3.S3UploadError):
                s3.normalize_settings(settings(expires_in=expiry))
        for expiry in (300, 604800):
            self.assertEqual(s3.normalize_settings(settings(expires_in=expiry))["expires_in"], expiry)

    def test_base64_config_requires_bounded_utf8_json_object(self):
        encoded = base64.b64encode(json.dumps(settings()).encode()).decode()
        self.assertEqual(s3.decode_settings(encoded), s3.normalize_settings(settings()))
        for value in (None, "", "!invalid!", "A" * 24001,
                      base64.b64encode(b"\xff").decode(),
                      base64.b64encode(b"[]").decode(),
                      base64.b64encode(b"{").decode()):
            with self.subTest(value=str(value)[:40]), self.assertRaises(s3.S3UploadError):
                s3.decode_settings(value)

    def test_signer_receives_only_explicit_credentials_and_no_proxies(self):
        import boto3

        normalized = s3.normalize_settings(settings(session_token="test-session-token"))
        with patch.object(boto3, "client") as create:
            self.assertIs(s3.make_client(normalized), create.return_value)
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["aws_access_key_id"], "test-access-key")
        self.assertEqual(kwargs["aws_secret_access_key"], "test-secret-key")
        self.assertEqual(kwargs["aws_session_token"], "test-session-token")
        self.assertEqual(kwargs["config"].proxies, {})
        self.assertEqual(kwargs["config"].signature_version, "s3v4")


class RoutingTests(unittest.TestCase):
    def test_dns_must_contain_only_public_addresses(self):
        with patch.object(s3.socket, "getaddrinfo", return_value=address_result("8.8.8.8", "8.8.8.8")):
            self.assertEqual(s3._public_addresses("s3.example.com"), ["8.8.8.8"])
        for addresses in ((), ("127.0.0.1",), ("10.1.1.1",), ("169.254.169.254",),
                          ("::1",), ("fc00::1",), ("8.8.8.8", "192.168.1.1")):
            with self.subTest(addresses=addresses), \
                    patch.object(s3.socket, "getaddrinfo", return_value=address_result(*addresses)), \
                    self.assertRaises(s3.S3UploadError):
                s3._public_addresses("s3.example.com")
        with patch.object(s3.socket, "getaddrinfo", side_effect=socket.gaierror("private-details")), \
                self.assertRaises(s3.S3UploadError) as raised:
            s3._public_addresses("s3.example.com")
        self.assertNotIn("private-details", str(raised.exception))

    def test_signed_urls_cannot_escape_configured_origin(self):
        config = s3.normalize_settings(settings())
        for host in ("s3.example.com", "test-media.s3.example.com"):
            self.assertEqual(s3._validate_signed_url("https://" + host + "/a?signature=secret", config).hostname, host)
        for url in ("http://s3.example.com/a", "https://evil.example/a?secret",
                    "https://s3.example.com.evil.example/a", "https://s3.example.com:444/a",
                    "https://user:secret@s3.example.com/a", "https://s3.example.com/a#secret",
                    "https://other-bucket.s3.example.com/a"):
            with self.subTest(url=url), self.assertRaises(s3.S3UploadError) as raised:
                s3._validate_signed_url(url, config)
            self.assertNotIn("secret", str(raised.exception))

    def test_tls_connects_to_pinned_ip_with_original_hostname(self):
        context, raw_socket = Mock(), Mock()
        with patch.object(s3.ssl, "create_default_context", return_value=context), \
                patch.object(s3.socket, "create_connection", return_value=raw_socket) as connect:
            connection = s3.PinnedHTTPSConnection("s3.example.com", "8.8.8.8")
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 30)
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="s3.example.com")
        self.assertIs(connection.sock, context.wrap_socket.return_value)

    def test_failed_tls_handshake_closes_raw_socket(self):
        context, raw_socket = Mock(), Mock()
        context.wrap_socket.side_effect = OSError("handshake failed")
        with patch.object(s3.ssl, "create_default_context", return_value=context), \
                patch.object(s3.socket, "create_connection", return_value=raw_socket):
            connection = s3.PinnedHTTPSConnection("s3.example.com", "8.8.8.8")
            with self.assertRaises(OSError):
                connection.connect()
        raw_socket.close.assert_called_once()

    def test_connection_pins_validated_dns_and_preserves_signed_target(self):
        with patch.object(s3, "_public_addresses", return_value=["8.8.8.8"]) as dns, \
                patch.object(s3, "PinnedHTTPSConnection") as connection:
            actual, target = s3._open_connection(
                "https://s3.example.com/bucket/a%20b?X-Amz-Signature=test",
                s3.normalize_settings(settings()))
        dns.assert_called_once_with("s3.example.com")
        connection.assert_called_once_with("s3.example.com", "8.8.8.8")
        self.assertIs(actual, connection.return_value)
        self.assertEqual(target, "/bucket/a%20b?X-Amz-Signature=test")


class TransferTests(unittest.TestCase):
    def test_put_streams_exact_bytes_without_authorization_or_acl_headers(self):
        connection = Mock()
        connection.getresponse.return_value.status = 200
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "test.png")
            source.write_bytes(PNG)
            with patch.object(s3, "_open_connection", return_value=(connection, "/signed-target")):
                s3._put_file("url", {}, source, len(PNG), "image/png")
        connection.putrequest.assert_called_once_with("PUT", "/signed-target", skip_accept_encoding=True)
        self.assertEqual(b"".join(call.args[0] for call in connection.send.call_args_list), PNG)
        headers = dict(call.args for call in connection.putheader.call_args_list)
        self.assertEqual(headers, {"Content-Type": "image/png", "Content-Length": str(len(PNG))})
        connection.close.assert_called_once()

    def test_redirects_and_auth_failures_are_not_followed(self):
        for status in (301, 302, 307, 308, 401, 403, 404, 500):
            with self.subTest(status=status):
                connection = Mock()
                connection.getresponse.return_value.status = status
                with patch.object(s3, "_open_connection", return_value=(connection, "/signed?secret")) as opened, \
                        self.assertRaises(s3.S3UploadError) as raised:
                    s3._verify_url("url", {}, len(PNG), PNG[:64])
                opened.assert_called_once()
                connection.close.assert_called_once()
                self.assertNotIn("secret", str(raised.exception))

    def test_readback_checks_range_total_size_and_original_prefix(self):
        for status in (200, 206):
            with self.subTest(status=status):
                connection = Mock()
                response = connection.getresponse.return_value
                response.status = status
                response.getheader.return_value = str(len(PNG)) if status == 200 else f"bytes 0-63/{len(PNG)}"
                response.read.return_value = PNG[:64]
                with patch.object(s3, "_open_connection", return_value=(connection, "/signed-target")):
                    s3._verify_url("url", {}, len(PNG), PNG[:64])
                connection.request.assert_called_once_with("GET", "/signed-target", headers={"Range": "bytes=0-63"})
                response.read.assert_called_once_with(64)
                connection.close.assert_called_once()
        for header, body in (("bytes 0-63/999", PNG[:64]), (f"bytes 0-63/{len(PNG)}", b"wrong")):
            connection = Mock()
            response = connection.getresponse.return_value
            response.status, response.getheader.return_value, response.read.return_value = 206, header, body
            with patch.object(s3, "_open_connection", return_value=(connection, "/target")), \
                    self.assertRaises(s3.S3UploadError):
                s3._verify_url("url", {}, len(PNG), PNG[:64])
            connection.close.assert_called_once()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.events, self.paths = [], []
        self.client = Mock()
        self.client.generate_presigned_url.side_effect = self.sign
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.make = self.stack.enter_context(patch.object(s3, "make_client", return_value=self.client))
        self.put = self.stack.enter_context(patch.object(s3, "_put_file", side_effect=self.put_file))
        self.verify = self.stack.enter_context(patch.object(s3, "_verify_url", side_effect=self.verify_url))
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        original_temporary_file = s3.tempfile.NamedTemporaryFile
        self.stack.enter_context(patch.object(s3.tempfile, "NamedTemporaryFile",
                                 side_effect=lambda **kw: original_temporary_file(dir=self.directory, **kw)))

    def sign(self, operation, **kwargs):
        self.events.append(operation)
        return "https://s3.example.com/" + kwargs["Params"]["Key"] + "?test-signature=" + operation

    def put_file(self, url, config, path, size, content_type):
        self.events.append("put")
        self.paths.append(Path(path))
        self.assertEqual(Path(path).read_bytes(), PNG)
        self.assertEqual(size, len(PNG))
        self.assertEqual(content_type, "image/png")

    def verify_url(self, url, config, size, prefix):
        self.events.append("verify")
        self.assertTrue(self.paths[-1].exists())
        self.assertEqual((size, prefix), (len(PNG), PNG[:64]))

    def test_success_follows_upload_and_readback_then_cleans_temporary(self):
        result = s3.upload_media(io.BytesIO(PNG), len(PNG), "my image.PNG", settings())
        self.assertEqual(self.events, ["put_object", "put", "get_object", "verify"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["filename"], "my_image.png")
        self.assertEqual((result["size"], result["content_type"]), (len(PNG), "image/png"))
        self.assertTrue(result["key"].startswith("ltx-inputs/"))
        self.assertGreater(datetime.fromisoformat(result["expires_at"]), datetime.now(timezone.utc))
        self.assertFalse(list(Path(self.directory).iterdir()))
        self.client.close.assert_called_once()
        self.assertNotIn("test-secret-key", json.dumps(result))
        self.client.create_bucket.assert_not_called()
        self.client.delete_object.assert_not_called()
        put_args, get_args = self.client.generate_presigned_url.call_args_list
        self.assertEqual(put_args.kwargs["Params"]["ContentType"], "image/png")
        self.assertNotIn("ACL", put_args.kwargs["Params"])
        self.assertEqual(get_args.kwargs["ExpiresIn"], 86400)

    def test_same_filename_generates_distinct_safe_object_keys(self):
        first = s3.upload_media(io.BytesIO(PNG), len(PNG), "same.png", settings(prefix="project/inputs"))
        second = s3.upload_media(io.BytesIO(PNG), len(PNG), "same.png", settings(prefix="project/inputs"))
        self.assertNotEqual(first["key"], second["key"])
        self.assertTrue(first["key"].startswith("project/inputs/"))
        self.assertNotIn("..", first["key"])

    def test_invalid_magic_and_truncated_stream_never_upload(self):
        for content, length, name in ((b"<script>" * 20, 160, "image.png"),
                                      (PNG, len(PNG), "wrong.jpg"),
                                      (PNG[:20], len(PNG), "image.png"),
                                      (PNG, len(PNG) + 100, "image.png")):
            with self.subTest(name=name, length=length), self.assertRaises(s3.S3UploadError):
                s3.upload_media(io.BytesIO(content), length, name, settings())
            self.assertFalse(list(Path(self.directory).iterdir()))
        self.put.assert_not_called()
        self.verify.assert_not_called()

    def test_oversized_and_unsafe_filenames_fail_before_reading(self):
        stream = Mock()
        stream.read.side_effect = AssertionError("invalid uploads must not be read")
        for size, name in ((s3.MAX_IMAGE_BYTES + 1, "image.png"),
                           (s3.MAX_MEDIA_BYTES + 1, "video.mp4"), (0, "empty.png"),
                           (100, "../escape.png"), (100, "C:\\fakepath\\image.png"),
                           (100, "script.html"), (True, "bad.png")):
            with self.subTest(size=size, name=name), self.assertRaises(s3.S3UploadError):
                s3.upload_media(stream, size, name, settings())
        stream.read.assert_not_called()
        self.make.assert_not_called()

    def test_upload_failure_redacts_sdk_details_and_cleans_temp(self):
        self.put.side_effect = RuntimeError("test-secret-key https://signed.example/?credential=test-access-key")
        with self.assertRaises(s3.S3UploadError) as raised:
            s3.upload_media(io.BytesIO(PNG), len(PNG), "test.png", settings())
        self.assertNotIn("test-secret-key", str(raised.exception))
        self.assertNotIn("test-access-key", str(raised.exception))
        self.assertFalse(list(Path(self.directory).iterdir()))
        self.verify.assert_not_called()
        self.client.delete_object.assert_not_called()
        self.client.close.assert_called_once()

    def test_readback_failure_never_reports_success_or_deletes_remote(self):
        self.verify.side_effect = s3.S3UploadError("Read verification denied.", 403)
        with self.assertRaises(s3.S3UploadError) as raised:
            s3.upload_media(io.BytesIO(PNG), len(PNG), "test.png", settings())
        self.assertEqual(raised.exception.status, 403)
        self.assertIn("retained", str(raised.exception))
        self.assertFalse(list(Path(self.directory).iterdir()))
        self.client.delete_object.assert_not_called()

    def test_client_close_failure_cannot_skip_temp_cleanup_or_leak_secrets(self):
        self.client.close.side_effect = RuntimeError("test-secret-key")
        self.verify.side_effect = s3.S3UploadError("Read verification denied.", 403)
        with self.assertRaises(s3.S3UploadError) as raised:
            s3.upload_media(io.BytesIO(PNG), len(PNG), "test.png", settings())
        self.assertNotIn("test-secret-key", str(raised.exception))
        self.assertFalse(list(Path(self.directory).iterdir()))

    def test_stream_error_still_closes_client_and_removes_temp(self):
        stream = Mock()
        stream.read.side_effect = [PNG[:64], OSError("test-secret-key")]
        with self.assertRaises(s3.S3UploadError) as raised:
            s3.upload_media(stream, len(PNG), "test.png", settings())
        self.assertNotIn("test-secret-key", str(raised.exception))
        self.assertFalse(list(Path(self.directory).iterdir()))
        self.client.close.assert_called_once()
        self.put.assert_not_called()


class RawHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("s3_route_server_under_test",
                                                    MODULE_PATH.with_name("server.py"))
        cls.server_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server_module)
        cls.httpd = cls.server_module.make_server(port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.received = []
        self.patcher = patch.object(self.server_module.s3_upload, "upload_media",
                                    side_effect=self.accept_upload)
        self.upload = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def accept_upload(self, stream, length, filename, config):
        total, prefix = 0, b""
        while total < length:
            chunk = stream.read(min(65536, length - total))
            if not chunk:
                raise self.server_module.s3_upload.S3UploadError("Incomplete.")
            prefix = (prefix + chunk)[:64]
            total += len(chunk)
        self.received.append((total, prefix, filename, config))
        return {"url": "https://s3.example.com/signed.png", "verified": True, "size": total}

    def request(self, body=PNG, changes=None):
        headers = {
            "Content-Type": "application/octet-stream",
            "X-S3-Config": base64.b64encode(json.dumps(settings()).encode()).decode(),
            "X-Upload-Name": urllib.parse.quote("my image.png"),
        }
        for key, value in (changes or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/api/s3/upload", body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def validate_request(self, document):
        body = document if isinstance(document, bytes) else json.dumps(document).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/api/s3/validate", body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_large_invalid_endpoint_upload_returns_json_instead_of_reset(self):
        # Regression: a Windows close with >64 KiB unread discarded the useful
        # settings error, causing XHR.onerror and an ambiguous upload warning.
        content = PNG + b"\x00" * (7 * 1024 * 1024 - len(PNG))
        invalid = settings(endpoint_url="https://account.r2.cloudflarestorage.com/az-ai-staging",
                           bucket="az-ai-staging", region="auto")
        encoded = base64.b64encode(json.dumps(invalid).encode()).decode()
        status, result = self.request(content, changes={"X-S3-Config": encoded})
        self.assertEqual(status, 400, result)
        self.assertIn("no bucket path", result["error"])
        self.assertFalse(result["submission_uncertain"])
        self.upload.assert_not_called()

    def test_validation_checks_settings_without_upload_signing_or_dns(self):
        endpoint = "https://account.r2.cloudflarestorage.com"
        cases = [
            (settings(endpoint_url=endpoint, region="auto"), 200, None),
            (settings(endpoint_url=endpoint, region="us-east-1"), 200, None),
            (settings(endpoint_url=endpoint + "/bucket", region="auto"), 400, "no bucket path"),
            (settings(endpoint_url=endpoint, region="WEUR"), 400, "signing region"),
            (settings(endpoint_url=endpoint, region="auto", secret_access_key=""),
             400, "secret access key"),
        ]
        with patch.object(self.server_module.s3_upload, "make_client") as make, \
                patch.object(self.server_module.s3_upload, "_public_addresses") as dns:
            for config, expected, message in cases:
                with self.subTest(expected=expected, message=message):
                    status, result = self.validate_request({
                        "settings": config, "filename": "image.png", "size": 7 * 1024 * 1024,
                    })
                    self.assertEqual(status, expected, result)
                    if expected == 200:
                        self.assertEqual(result, {"valid": True})
                    else:
                        self.assertIn(message, result["error"])
                    self.assertNotIn("test-secret-key", json.dumps(result))
                    self.assertNotIn("test-access-key", json.dumps(result))
            make.assert_not_called()
            dns.assert_not_called()
        self.upload.assert_not_called()

    def test_validation_rejects_malformed_metadata_and_oversized_json(self):
        good = {"settings": settings(), "filename": "image.png", "size": 100}
        cases = [({}, 400), ({**good, "unexpected": True}, 400),
                 ({**good, "settings": None}, 400),
                 ({**good, "filename": "../image.png"}, 400),
                 ({**good, "filename": 42}, 400), ({**good, "size": "100"}, 413),
                 ({**good, "size": 0}, 413), ({**good, "size": True}, 413),
                 ({**good, "size": s3.MAX_IMAGE_BYTES + 1}, 413),
                 (b"[]", 400), (b"{", 400), (b" " * 32769, 413)]
        with patch.object(self.server_module.s3_upload, "make_client") as make:
            for document, expected in cases:
                with self.subTest(document=str(document)[:100]):
                    status, result = self.validate_request(document)
                    self.assertEqual(status, expected, result)
                    self.assertTrue(result["error"])
                    self.assertFalse(result["submission_uncertain"])
            make.assert_not_called()
        self.upload.assert_not_called()

    def test_large_binary_image_bypasses_json_and_base64_limits(self):
        content = PNG + b"\x00" * (11 * 1024 * 1024 - len(PNG))
        status, result = self.request(content)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["size"], 11 * 1024 * 1024)
        size, prefix, filename, config = self.received[0]
        self.assertEqual((size, prefix, filename), (len(content), PNG[:64], "my image.png"))
        self.assertEqual(config["endpoint_url"], "https://s3.example.com")
        self.upload.assert_called_once()

    def test_raw_route_rejects_missing_or_bad_config_before_upload(self):
        for headers in ({"X-S3-Config": None}, {"X-Upload-Name": None},
                        {"X-S3-Config": "bad-base64"}, {"X-Upload-Name": "%FF"}):
            with self.subTest(headers=headers):
                status, result = self.request(changes=headers)
                self.assertEqual(status, 400, result)
                self.assertTrue(result["error"])
        self.upload.assert_not_called()

    def test_raw_upload_retains_same_origin_guards(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _ = self.request(changes=headers)
                self.assertEqual(status, 403)
        self.upload.assert_not_called()

    def test_raw_route_rejects_empty_and_excessive_declared_sizes(self):
        for size in ("0", str(s3.MAX_MEDIA_BYTES + 1)):
            with self.subTest(size=size):
                status, _ = self.request(body=b"", changes={"Content-Length": size})
                self.assertEqual(status, 413)
        self.upload.assert_not_called()

    def test_upload_slots_are_bounded_and_released_after_failure(self):
        self.assertTrue(self.httpd.s3_slots.acquire(blocking=False))
        self.assertTrue(self.httpd.s3_slots.acquire(blocking=False))
        try:
            status, _ = self.request()
            self.assertEqual(status, 429)
            self.upload.assert_not_called()
        finally:
            self.httpd.s3_slots.release()
            self.httpd.s3_slots.release()
        self.upload.side_effect = self.server_module.s3_upload.S3UploadError("Denied.", 403)
        status, result = self.request()
        self.assertEqual(status, 403, result)
        self.assertTrue(self.httpd.s3_slots.acquire(blocking=False))
        self.assertTrue(self.httpd.s3_slots.acquire(blocking=False))
        self.httpd.s3_slots.release()
        self.httpd.s3_slots.release()


if __name__ == "__main__":
    unittest.main()
