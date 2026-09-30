import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("download_models", ROOT / "scripts/download_models.py")
MODELS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODELS)


class ModelBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b"sample model data"
        self.entry = dict(repo="Lightricks/example", revision="a" * 40, filename="model.safetensors",
                          destination="vae/model.safetensors", size=len(self.payload),
                          sha256=hashlib.sha256(self.payload).hexdigest())

    def write_manifest(self, entry):
        path = self.root / "manifest.json"
        path.write_text(json.dumps({"schema_version": 1, "models": [entry]}))
        return path

    def test_real_manifest_pins_every_model(self):
        entries = MODELS.load_manifest(ROOT / "models/manifest.json")
        self.assertEqual(len(entries), 11)
        self.assertEqual(sum(x["size"] for x in entries), 44542597655)

    def test_rejects_mutable_revision(self):
        self.entry["revision"] = "main"
        with self.assertRaises(MODELS.DownloadError):
            MODELS.load_manifest(self.write_manifest(self.entry))

    def test_rejects_traversal(self):
        self.entry["destination"] = "../outside.safetensors"
        with self.assertRaises(MODELS.DownloadError):
            MODELS.load_manifest(self.write_manifest(self.entry))

    def test_detects_same_size_corruption(self):
        path = MODELS.model_path(self.root, self.entry)
        path.parent.mkdir()
        path.write_bytes(b"x" * len(self.payload))
        self.assertTrue(MODELS.verify_file(path, self.entry, full_hash=False))
        self.assertFalse(MODELS.verify_file(path, self.entry))

    def test_cross_host_redirect_strips_token(self):
        request = Request("https://huggingface.co/a", headers={"Authorization": "Bearer private"})
        redirected = MODELS.SafeRedirects().redirect_request(request, None, 302, "Found", {}, "https://cdn.example/model")
        self.assertFalse(redirected.has_header("Authorization"))

    def test_rejects_http_redirect(self):
        request = Request("https://huggingface.co/a")
        with self.assertRaises(MODELS.DownloadError):
            MODELS.SafeRedirects().redirect_request(request, None, 302, "Found", {}, "http://cdn.example/model")

    def test_atomic_download_and_hash_verification(self):
        response = io.BytesIO(self.payload)
        response.status = 200
        with patch.object(MODELS, "build_opener") as opener:
            opener.return_value.open.return_value = response
            MODELS.download(self.entry, self.root, "", attempts=1)
        self.assertTrue(MODELS.verify_file(MODELS.model_path(self.root, self.entry), self.entry))
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_invalid_download_never_promotes_partial(self):
        response = io.BytesIO(b"x" * len(self.payload))
        response.status = 200
        with patch.object(MODELS, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(MODELS.DownloadError):
                MODELS.download(self.entry, self.root, "", attempts=1)
        self.assertFalse(MODELS.model_path(self.root, self.entry).exists())
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_access_probe_uses_head_and_accepts_200(self):
        response = io.BytesIO()
        response.status, response.headers = 200, {}
        with patch.object(MODELS, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertIsNone(MODELS.probe_access(self.entry, "private", attempts=1))
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertIn("/" + self.entry["revision"] + "/", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer private")

    def test_access_probe_accepts_https_redirect_without_following_it(self):
        signed = "https://cdn.example/model?Signature=must-not-appear"
        error = HTTPError("https://huggingface.co/model", 302, "Found", {"Location": signed}, None)
        with patch.object(MODELS, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            self.assertIsNone(MODELS.probe_access(self.entry, "private", attempts=1))
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertIsInstance(opener.call_args.args[0], MODELS.NoAccessRedirects)
        request = Request("https://huggingface.co/model")
        self.assertIsNone(MODELS.NoAccessRedirects().redirect_request(
            request, None, 302, "Found", {}, signed))

    def test_access_redirect_requires_https_host_without_credentials(self):
        request = Request("https://huggingface.co/model")
        for target in (None, "http://cdn.example/file", "https://user:private@cdn.example/file",
                       "https://cdn.example:8443/file"):
            with self.subTest(target=target):
                self.assertFalse(MODELS.valid_access_redirect(request, target))
        self.assertTrue(MODELS.valid_access_redirect(request, "/api/resolve-cache/example"))

    def test_access_preflight_lists_all_denied_and_missing_files(self):
        entries = []
        errors = []
        for number, status in enumerate((401, 403, 404)):
            entries.append(dict(self.entry, repo=f"Lightricks/repository{number}",
                                filename=f"model{number}.safetensors",
                                destination=f"vae/model{number}.safetensors"))
            errors.append(HTTPError("https://cdn.example/?Signature=private", status,
                                    "private-token-must-not-appear", {}, None))
        with patch.object(MODELS, "build_opener") as opener:
            opener.return_value.open.side_effect = errors
            with self.assertRaises(MODELS.DownloadError) as caught:
                MODELS.check_access(entries, self.root, "private-token", attempts=1)
            self.assertEqual(opener.return_value.open.call_count, 3)
        message = str(caught.exception)
        for item in ("Lightricks/repository0", "Lightricks/repository1",
                     "Lightricks/repository2/model2.safetensors"):
            self.assertIn(item, message)
        self.assertIn("No models were downloaded", message)
        self.assertNotIn("Signature", message)
        self.assertNotIn("private-token", message)
        self.assertFalse((self.root / "vae").exists())

    def test_access_probe_retries_transient_failure_then_succeeds(self):
        response = io.BytesIO()
        response.status, response.headers = 200, {}
        error = HTTPError("https://huggingface.co/model", 503, "Unavailable", {}, None)
        with patch.object(MODELS, "build_opener") as opener, patch.object(MODELS.time, "sleep") as sleep:
            opener.return_value.open.side_effect = [error, response]
            self.assertIsNone(MODELS.probe_access(self.entry, "", attempts=3))
            self.assertEqual(opener.return_value.open.call_count, 2)
            sleep.assert_called_once_with(5)

    def test_access_network_errors_are_bounded_and_sanitized(self):
        with patch.object(MODELS, "build_opener") as opener, patch.object(MODELS.time, "sleep") as sleep:
            opener.return_value.open.side_effect = URLError("https://cdn.example/?Signature=private")
            result = MODELS.probe_access(self.entry, "private", attempts=3)
            self.assertEqual(result, ("failed", "URLError"))
            self.assertEqual(opener.return_value.open.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_access_check_skips_only_hash_verified_cached_files(self):
        path = MODELS.model_path(self.root, self.entry)
        path.parent.mkdir()
        path.write_bytes(self.payload)
        with patch.object(MODELS, "probe_access") as probe:
            self.assertEqual(MODELS.check_access([self.entry], self.root, ""), [])
            probe.assert_not_called()
        path.write_bytes(b"x" * len(self.payload))
        with patch.object(MODELS, "probe_access", return_value=None) as probe:
            self.assertEqual(MODELS.check_access([self.entry], self.root, ""), [self.entry])
            probe.assert_called_once()

    def test_access_only_cli_does_not_create_model_directory_or_download(self):
        manifest = self.write_manifest(self.entry)
        destination = self.root / "unused-model-directory"
        arguments = ["download_models.py", "--manifest", str(manifest), "--root", str(destination),
                     "--check-access-only"]
        with (patch("sys.argv", arguments), patch.object(MODELS, "probe_access", return_value=None),
              patch.object(MODELS, "download") as download):
            MODELS.main()
        download.assert_not_called()
        self.assertFalse(destination.exists())

    def test_failed_preflight_prevents_cli_bulk_downloads(self):
        manifest = self.write_manifest(self.entry)
        arguments = ["download_models.py", "--manifest", str(manifest), "--root", str(self.root)]
        with (patch("sys.argv", arguments),
              patch.object(MODELS, "probe_access", return_value=("denied", "HTTP 403")),
              patch.object(MODELS, "download") as download,
              patch("sys.stderr", new_callable=io.StringIO)):
            with self.assertRaises(SystemExit) as caught:
                MODELS.main()
        self.assertEqual(caught.exception.code, 1)
        download.assert_not_called()

    def test_fully_verified_cache_needs_no_free_disk_for_download(self):
        manifest = self.write_manifest(self.entry)
        path = MODELS.model_path(self.root, self.entry)
        path.parent.mkdir()
        path.write_bytes(self.payload)
        arguments = ["download_models.py", "--manifest", str(manifest), "--root", str(self.root)]
        with (patch("sys.argv", arguments), patch.object(MODELS, "probe_access") as probe,
              patch.object(MODELS.shutil, "disk_usage") as disk,
              patch.object(MODELS, "download") as download):
            MODELS.main()
        probe.assert_not_called()
        disk.assert_not_called()
        download.assert_not_called()

    def test_download_reports_byte_progress_without_sensitive_urls(self):
        response = io.BytesIO(self.payload)
        response.status = 200
        with (patch.object(MODELS, "build_opener") as opener,
              patch.object(MODELS.time, "monotonic", side_effect=[0, 31]),
              patch("sys.stdout", new_callable=io.StringIO) as output):
            opener.return_value.open.return_value = response
            MODELS.download(self.entry, self.root, "private-token", attempts=1)
        self.assertIn("Download progress: vae/model.safetensors 17/17 bytes", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("https://", output.getvalue())

    def test_default_download_all_preserves_sequential_calls(self):
        second = dict(self.entry, destination="vae/second.safetensors")
        with patch.object(MODELS, "download") as download:
            MODELS.download_all([self.entry, second], self.root, "private")
        self.assertEqual(download.call_args_list,
                         [call(self.entry, self.root, "private"),
                          call(second, self.root, "private")])

    def test_parallel_worker_limit_and_cli_bounds(self):
        for workers in (0, 5, -1, True, 1.5):
            with self.subTest(workers=workers), self.assertRaises(MODELS.DownloadError):
                MODELS.download_all([self.entry], self.root, "", workers=workers)
        for workers in ("0", "5", "bad"):
            with patch("sys.argv", ["download_models.py", "--workers", workers]), \
                    patch("sys.stderr", new_callable=io.StringIO), \
                    self.assertRaises(SystemExit) as caught:
                MODELS.main()
            self.assertEqual(caught.exception.code, 2)

    def test_parallel_downloads_match_serial_hashes_and_bound_concurrency(self):
        entries = [dict(self.entry, filename=f"model{i}.safetensors",
                        destination=f"vae/model{i}.safetensors") for i in range(6)]
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = 0
        peak = 0
        parallel = False
        payload = self.payload

        class Response(io.BytesIO):
            status = 200

            def __enter__(response):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                if parallel:
                    barrier.wait(timeout=5)
                return response

            def __exit__(response, *args):
                nonlocal active
                with lock:
                    active -= 1
                response.close()

        with patch.object(MODELS, "build_opener") as opener, \
                patch("sys.stdout", new_callable=io.StringIO) as logged:
            opener.return_value.open.side_effect = lambda *args, **kwargs: Response(payload)
            serial_root = self.root / "serial"
            MODELS.download_all(entries, serial_root, "private-token")
            parallel = True
            parallel_root = self.root / "parallel"
            MODELS.download_all(entries, parallel_root, "private-token", workers=3)
        self.assertEqual(peak, 3)
        self.assertEqual(active, 0)
        for entry in entries:
            serial = MODELS.model_path(serial_root, entry)
            concurrent = MODELS.model_path(parallel_root, entry)
            self.assertEqual(serial.read_bytes(), concurrent.read_bytes())
            self.assertTrue(MODELS.verify_file(concurrent, entry))
        self.assertFalse(list(self.root.rglob("*.partial")))
        self.assertNotIn("private-token", logged.getvalue())
        self.assertNotIn("https://", logged.getvalue())

    def test_parallel_failure_stops_running_and_pending_without_leaking_error(self):
        entries = [dict(self.entry, destination=f"vae/model{i}.safetensors")
                   for i in range(6)]
        second_started = threading.Event()
        running_stopped = threading.Event()
        visited = []

        def transfer(entry, root, token, attempts=3, stop_event=None):
            visited.append(entry["destination"])
            if entry is entries[0]:
                self.assertTrue(second_started.wait(5))
                raise URLError("https://cdn.example/?Signature=SECRET-private-token")
            second_started.set()
            self.assertTrue(stop_event.wait(5))
            running_stopped.set()
            raise MODELS.DownloadCancelled("cancelled")

        with patch.object(MODELS, "download", side_effect=transfer), \
                patch("sys.stdout", new_callable=io.StringIO) as logged:
            with self.assertRaises(MODELS.DownloadError) as caught:
                MODELS.download_all(entries, self.root, "private-token", workers=2)
        self.assertTrue(running_stopped.is_set())
        self.assertEqual(set(visited), {entries[0]["destination"], entries[1]["destination"]})
        self.assertIn(entries[0]["destination"], str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception) + logged.getvalue())
        self.assertNotIn("private-token", str(caught.exception) + logged.getvalue())

    def test_cancel_during_chunk_removes_partial_without_retry_or_promotion(self):
        stop = threading.Event()
        payload = self.payload

        class Response(io.BytesIO):
            status = 200

            def read(response, size=-1):
                value = super().read(size)
                stop.set()
                return value

        with patch.object(MODELS, "build_opener") as opener, \
                patch.object(MODELS.time, "sleep") as sleep:
            opener.return_value.open.return_value = Response(payload)
            with self.assertRaises(MODELS.DownloadCancelled):
                MODELS.download(self.entry, self.root, "", stop_event=stop)
        self.assertEqual(opener.return_value.open.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(MODELS.model_path(self.root, self.entry).exists())
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_cancellation_before_retry_does_not_start_another_request(self):
        stop = threading.Event()

        def failure(*args, **kwargs):
            stop.set()
            raise URLError("https://cdn.example/?Signature=private")

        with patch.object(MODELS, "build_opener") as opener, \
                patch.object(MODELS.time, "sleep") as sleep:
            opener.return_value.open.side_effect = failure
            with self.assertRaises(MODELS.DownloadCancelled):
                MODELS.download(self.entry, self.root, "", stop_event=stop)
        self.assertEqual(opener.return_value.open.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_conflicting_alias_partial_and_parent_paths_fail_before_download(self):
        conflicts = ["vae/./model.safetensors", "vae/model.safetensors.partial",
                     "vae/model.safetensors/child"]
        for destination in conflicts:
            entries = [self.entry, dict(self.entry, destination=destination)]
            with self.subTest(destination=destination), \
                    patch.object(MODELS, "download") as download, \
                    self.assertRaises(MODELS.DownloadError):
                MODELS.download_all(entries, self.root, "", workers=3)
            download.assert_not_called()

    def test_parallel_cli_keeps_access_and_total_disk_checks_before_transfers(self):
        entries = [self.entry, dict(self.entry, destination="vae/second.safetensors")]
        manifest = self.write_manifest(self.entry)
        manifest.write_text(json.dumps({"schema_version": 1, "models": entries}))
        arguments = ["download_models.py", "--manifest", str(manifest),
                     "--root", str(self.root), "--workers", "3"]
        events = []
        def access(*args):
            events.append("access")
            return entries
        def disk(*args):
            events.append("disk")
            return type("Usage", (), {"free": 1024**3 + 2 * len(self.payload)})()
        def transfers(*args, **kwargs):
            events.append("transfers")
            self.assertEqual(kwargs["workers"], 3)
        with patch("sys.argv", arguments), \
                patch.object(MODELS, "check_access", side_effect=access), \
                patch.object(MODELS.shutil, "disk_usage", side_effect=disk), \
                patch.object(MODELS, "download_all", side_effect=transfers):
            MODELS.main()
        self.assertEqual(events, ["access", "disk", "transfers"])


if __name__ == "__main__":
    unittest.main()
