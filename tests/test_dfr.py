import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from ltx_worker import dfr
from ltx_worker.errors import InputError


PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAYCAIAAAAH5iiXAAAAJ0lEQVR4nO3NMREAAAgE"
    "ILV/Z03h/QIF6K2MCb1isVgsFovFYvGDA2f1AS9M68qOAAAAAElFTkSuQmCC"
)


def request(image=False, **parameters):
    value = {
        "mode": "image_to_video_dfr_4k" if image else "text_to_video_dfr_4k",
        "prompt": "A calm cinematic scene.", "parameters": parameters,
    }
    if image:
        value["media"] = {"image": "data:image/png;base64," + PNG}
    return value


class DfrContractTests(unittest.TestCase):
    def test_defaults_and_metadata_are_independent(self):
        value = dfr.validate_input(request())
        self.assertEqual(value["parameters"], dfr.DEFAULT_PARAMETERS)
        self.assertEqual(value["parameters"]["num_frames"], 33)
        specs = dfr.mode_specs()
        specs["text_to_video_dfr_4k"]["defaults"]["num_frames"] = 999
        self.assertEqual(dfr.mode_specs()["text_to_video_dfr_4k"]
                         ["defaults"]["num_frames"], 33)
        self.assertEqual(set(specs), dfr.DFR_MODES)

    def test_all_frame_counts_and_independent_image_sources(self):
        sources = [PNG, "data:image/png;base64," + PNG,
                   {"base64": PNG}, {"url": "https://s3.example/input?signature=private"}]
        for frames in (9, 33, 121):
            for source in sources:
                with self.subTest(frames=frames, source_type=type(source)):
                    value = request(image=True, num_frames=frames)
                    value["media"]["image"] = source
                    normalized = dfr.validate_input(value)
                    self.assertEqual(normalized["media"]["image"], source)
                    self.assertEqual(normalized["parameters"]["image_strength"], 0.8)

    def test_settings_reject_bool_nonfinite_unknown_and_wrong_shape(self):
        bad = [
            {"num_frames": 17}, {"num_frames": True}, {"fps": 30},
            {"width": 1920}, {"height": 2160}, {"seed": -1},
            {"seed": 2**32}, {"seed": 10**1000}, {"seed": 42.0},
            {"image_strength": float("nan")}, {"image_strength": float("inf")},
            {"image_strength": True}, {"image_strength": 1.1},
            {"steps": 8}, {"lora_strength": 0.5}, {"quantization": "fp8-cast"},
        ]
        for parameters in bad:
            with self.subTest(parameters=list(parameters)), self.assertRaises(InputError):
                dfr.validate_input(request(image=True, **parameters))

    def test_missing_media_and_unconsumed_fields_fail_pure_validation(self):
        values = [
            None, {}, {"mode": "unrecognized", "prompt": "ok"},
            dict(request(), prompt=""), dict(request(), prompt="\x00secret"),
            dict(request(), prompt="x" * 12001),
            dict(request(), parameters=[]), dict(request(), media=[]),
            dict(request(), negative_prompt="unused"),
            dict(request(), workflow={}),
            dict(request(), media={"image": PNG}),
            dict(request(image=True), media={}),
            dict(request(image=True), media={"image": {"url": "a", "base64": "b"}}),
            dict(request(image=True), media={"image": 1}),
        ]
        for value in values:
            with self.subTest(value_type=type(value)), self.assertRaises(InputError):
                dfr.validate_input(value)


class DfrExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.models = Path(self.temp.name) / "models"
        for relative in dfr.MODEL_FILES.values():
            path = self.models / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model-fixture")
        self.addCleanup(patch.stopall)
        patch.object(dfr, "MODEL_ROOT", self.models).start()
        patch.dict(os.environ, {"BUCKET_ENDPOINT_URL": "https://storage.example",
                                "DFR_MODEL_ROOT": str(self.models),
                                "DFR_EXECUTION_TIMEOUT_S": "3600",
                                "MAX_DFR_OUTPUT_BYTES": "1048576",
                                "MAX_RESULT_BYTES": "8388608",
                                "MEDIA_PROCESS_TIMEOUT_S": "180"}).start()
        self.credit = {"total_cost_usd": 0, "credits_spent": 0, "nodes": []}
        self.storage = SimpleNamespace(
            MAX_INLINE_OUTPUT_BYTES=1024,
            validate_output_storage=Mock(),
            build_empty_credit_usage=Mock(return_value=self.credit),
            _upload_output_bytes=Mock(return_value="https://results.example/output.mp4"),
        )
        self.job = {"id": "original-runpod-id", "input": request()}
        self.workdir = None
        self.commands = []
        self.bad_probe = None
        self.failure_phase = None
        self.frames = 33
        self.runner = patch.object(dfr, "_run_command", side_effect=self.run_command).start()

    def run_command(self, command, timeout, phase, notify=None, capture=False):
        self.commands.append((phase, command, timeout))
        if phase == "DFR pipeline":
            output = Path(command[command.index("--output-path") + 1])
            self.workdir = output.parent
            self.frames = int(command[command.index("--num-frames") + 1])
            output.write_bytes(b"raw-video-fixture")
        if phase == self.failure_phase:
            raise InputError("Controlled pipeline failure", "DFR_EXECUTION_FAILED")
        if phase == "UHD cropping":
            Path(command[-1]).write_bytes(b"verified-video-fixture")
        if phase == "Video verification":
            height = 2176 if Path(command[-1]).name == "generated.mp4" else 2160
            stream = {
                "codec_name": "h264", "width": 3840, "height": height,
                "nb_read_frames": str(self.frames), "avg_frame_rate": "24/1",
                "duration": str(self.frames / 24),
            }
            if self.bad_probe and self.bad_probe[0] == height:
                stream.update(self.bad_probe[1])
            return json.dumps({"streams": [stream]}).encode()
        return b""

    def test_official_cli_s3_contract_and_cleanup_after_delivery(self):
        def upload(job_id, filename, content):
            self.assertEqual(job_id, self.job["id"])
            self.assertEqual(filename, "LTX25-DFR.mp4")
            self.assertEqual(content, b"verified-video-fixture")
            self.assertTrue((self.workdir / filename).is_file())
            self.assertTrue((self.workdir / "generated.mp4").is_file())
            return "https://results.example/result.mp4"
        self.storage._upload_output_bytes.side_effect = upload
        progress = Mock()
        result = dfr.execute(self.job, storage=self.storage, progress=progress)
        self.assertTrue(result["success"])
        self.assertEqual(result["job_id"], self.job["id"])
        uuid.UUID(result["prompt_id"])
        self.assertEqual(result["backend"]["prompt_id_kind"], "worker_execution_uuid")
        self.assertEqual(result["credit_usage"], dict(self.credit, prompt_id=result["prompt_id"]))
        self.assertFalse(result["comfy_credits"]["available"])
        self.assertEqual(result["videos"][0]["type"], "s3_url")
        self.assertEqual(result["videos"][0]["media_type"], "video")
        self.assertFalse(self.workdir.exists())
        self.assertEqual([p for p, _, _ in self.commands],
                         ["DFR pipeline", "Video verification", "UHD cropping",
                          "Video verification"])
        command = self.commands[0][1]
        self.assertEqual(command[:3], [dfr.sys.executable, "-m", "ltx_pipelines.dfr_pipeline"])
        for flag, relative in dfr.MODEL_FILES.items():
            self.assertEqual(command[command.index("--" + flag) + 1],
                             str(self.models / relative))
        self.assertEqual(command[command.index("--offload") + 1], "cpu")
        self.assertEqual(command[command.index("--spatial-upscalings") + 1], "2")
        self.assertEqual(command[command.index("--temporal-upscalings") + 1], "0")
        self.assertNotIn("--quantization", command)
        self.assertNotIn("--diffvae-optimization", command)
        self.assertNotIn("--image", command)
        crop = self.commands[2][1]
        self.assertIn("crop=3840:2160:0:8", crop)
        self.assertEqual(crop[crop.index("-crf") + 1], "18")
        self.assertEqual(crop[crop.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(crop[crop.index("-c:a") + 1], "copy")
        self.assertGreaterEqual(progress.call_count, 3)

    def test_image_data_uri_ingestion_and_image_cli(self):
        self.job["input"] = request(image=True, image_strength=0.6, num_frames=9)
        result = dfr.execute(self.job, storage=self.storage)
        self.assertTrue(result["success"])
        command = self.commands[0][1]
        i = command.index("--image")
        self.assertEqual(Path(command[i + 1]).name, "image.png")
        self.assertEqual(command[i + 2:i + 4], ["0", "0.6"])
        self.assertEqual(self.frames, 9)
        self.assertFalse(self.workdir.exists())

    def test_s3_source_uses_shared_ingestion_without_leaking_url(self):
        self.job["input"] = request(image=True)
        url = "https://input.example/image?signature=SECRET"
        self.job["input"]["media"]["image"] = {"url": url}
        def ingest(source, kind, directory, role, params):
            self.assertEqual(source, {"url": url})
            directory.mkdir(parents=True)
            path = directory / "image.png"
            path.write_bytes(base64.b64decode(PNG))
            return path
        with patch("ltx_worker.media.ingest", side_effect=ingest) as mocked:
            result = dfr.execute(self.job, storage=self.storage)
        self.assertTrue(result["success"])
        mocked.assert_called_once()
        self.assertNotIn("SECRET", repr(self.commands))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_upload_failure_has_no_success_or_artifact_and_cleans_temps(self):
        self.storage._upload_output_bytes.side_effect = RuntimeError("signed-url=SECRET")
        result = dfr.execute(self.job, storage=self.storage)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "OUTPUT_UPLOAD_FAILED")
        self.assertNotIn("videos", result)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(self.workdir.exists())

    def test_partial_storage_config_fails_before_generation(self):
        self.storage.validate_output_storage.side_effect = ValueError("SECRET")
        result = dfr.execute(self.job, storage=self.storage)
        self.assertEqual(result["error_code"], "OUTPUT_STORAGE_CONFIGURATION")
        self.runner.assert_not_called()
        self.storage._upload_output_bytes.assert_not_called()
        self.assertNotIn("SECRET", json.dumps(result))

    def test_wrong_raw_or_final_media_never_uploads(self):
        for height in (2176, 2160):
            for change in ({"width": 1920}, {"nb_read_frames": "9"},
                           {"avg_frame_rate": "30/1"}, {"duration": "nan"}):
                with self.subTest(height=height, change=change):
                    self.bad_probe = (height, change)
                    result = dfr.execute(self.job, storage=self.storage)
                    self.assertEqual(result["error_code"], "OUTPUT_VALIDATION_FAILED")
                    self.assertFalse(self.workdir.exists())
        self.storage._upload_output_bytes.assert_not_called()

    def test_generation_and_crop_failures_cleanup_without_upload(self):
        for phase in ("DFR pipeline", "UHD cropping"):
            with self.subTest(phase=phase):
                self.failure_phase = phase
                result = dfr.execute(self.job, storage=self.storage)
                self.assertFalse(result["success"])
                self.assertFalse(self.workdir.exists())
                self.assertNotIn("videos", result)
        self.storage._upload_output_bytes.assert_not_called()

    def test_base64_fallback_and_inline_limits(self):
        with patch.dict(os.environ, {"BUCKET_ENDPOINT_URL": ""}):
            result = dfr.execute(self.job, storage=self.storage)
            self.assertTrue(result["success"])
            video = result["videos"][0]
            self.assertEqual(video["type"], "base64")
            self.assertEqual(base64.b64decode(video["data"]), b"verified-video-fixture")
            self.assertIn("warning", video)
            self.storage.MAX_INLINE_OUTPUT_BYTES = 1
            result = dfr.execute(self.job, storage=self.storage)
            self.assertEqual(result["error_code"], "RESULT_TOO_LARGE")
            self.assertNotIn("videos", result)
        self.storage._upload_output_bytes.assert_not_called()
        self.assertFalse(self.workdir.exists())

    def test_response_limit_fails_without_inline_artifact(self):
        with patch.dict(os.environ, {"BUCKET_ENDPOINT_URL": "", "MAX_RESULT_BYTES": "10"}):
            result = dfr.execute(self.job, storage=self.storage)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "RESULT_TOO_LARGE")
        self.assertNotIn("videos", result)
        self.assertFalse(self.workdir.exists())

    def test_missing_model_invalid_media_and_timeout_configuration(self):
        model = self.models / next(iter(dfr.MODEL_FILES.values()))
        model.unlink()
        result = dfr.execute(self.job, storage=self.storage)
        self.assertEqual(result["error_code"], "MISSING_MODEL")
        self.runner.assert_not_called()
        self.job["input"] = request(image=True)
        self.job["input"]["media"]["image"] = "invalid!!"
        result = dfr.execute(self.job, storage=self.storage)
        self.assertFalse(result["success"])
        with patch.dict(os.environ, {"DFR_EXECUTION_TIMEOUT_S": "0"}):
            result = dfr.execute(self.job, storage=self.storage)
        self.assertEqual(result["error_code"], "WORKER_CONFIGURATION")
        self.storage._upload_output_bytes.assert_not_called()

    def test_progress_failure_does_not_change_delivery(self):
        result = dfr.execute(self.job, storage=self.storage,
                             progress=Mock(side_effect=RuntimeError("secret")))
        self.assertTrue(result["success"])


class DfrProcessTests(unittest.TestCase):
    def test_timeout_terminates_process_group_without_exposing_command(self):
        command = ["python", "--prompt=PRIVATE"]
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(command, 1)
        with patch.object(dfr.subprocess, "Popen", return_value=process) as popen, \
                patch.object(dfr.time, "monotonic", side_effect=[0, 0, 2]), \
                patch.object(dfr, "_terminate_process_tree") as terminate:
            with self.assertRaises(InputError) as raised:
                dfr._run_command(command, 1, "DFR pipeline")
        self.assertEqual(raised.exception.code, "DFR_TIMEOUT")
        self.assertNotIn("PRIVATE", str(raised.exception))
        terminate.assert_called_once_with(process)
        self.assertTrue(popen.call_args.kwargs["stderr"].closed)
        self.assertIs(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_nonzero_exit_redacted_and_tree_reaped(self):
        process = Mock(returncode=7)
        process.communicate.return_value = (b"", b"secret")
        with patch.object(dfr.subprocess, "Popen", return_value=process), \
                patch.object(dfr, "_terminate_process_tree") as terminate:
            with self.assertRaisesRegex(InputError, "exit code 7") as raised:
                dfr._run_command(["python", "secret"], 1, "DFR pipeline")
        self.assertNotIn("secret", str(raised.exception))
        terminate.assert_called_once_with(process)

    def test_posix_cleanup_signals_entire_group(self):
        process = Mock(pid=123456)
        with patch.object(dfr.os, "name", "posix"), \
                patch.object(dfr.os, "killpg", create=True) as killpg, \
                patch.object(dfr.signal, "SIGKILL", 9, create=True):
            dfr._terminate_process_tree(process)
        self.assertEqual(killpg.call_count, 2)
        self.assertEqual(killpg.call_args_list[0].args[0], process.pid)
        self.assertEqual(killpg.call_args_list[1].args[0], process.pid)

    def test_diagnostics_classification_is_bounded_private_and_temporary(self):
        cases = [
            (b"torch.OutOfMemoryError: CUDA out of memory", "GPU_OUT_OF_MEMORY"),
            (b"the provided PTX was compiled with an unsupported toolchain",
             "GPU_RUNTIME_INCOMPATIBLE"),
            (b"CUDA driver version is insufficient", "GPU_RUNTIME_INCOMPATIBLE"),
            (b"ModuleNotFoundError: private.package", "WORKER_CONFIGURATION"),
            (b"unrecognized failure", "DFR_EXECUTION_FAILED"),
        ]
        for diagnostic, code in cases:
            with self.subTest(code=code):
                process = Mock(returncode=1)
                process.communicate.return_value = (b"", None)
                opened = []
                def popen(*args, **kwargs):
                    opened.append(kwargs["stderr"])
                    kwargs["stderr"].write(b"secret-prompt-and-signed-url" + b"x" * 70000)
                    kwargs["stderr"].write(diagnostic)
                    kwargs["stderr"].flush()
                    return process
                with patch.object(dfr.subprocess, "Popen", side_effect=popen), \
                        patch.object(dfr, "_terminate_process_tree"):
                    with self.assertRaises(InputError) as raised:
                        dfr._run_command(["python"], 1, "DFR pipeline")
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("private.package", str(raised.exception))
                self.assertTrue(opened[0].closed)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg/FFprobe are required for the real CPU media check")
class DfrRealMediaTests(unittest.TestCase):
    def test_real_uhd_crop_preserves_nine_frames_fps_and_audio_packets(self):
        with tempfile.TemporaryDirectory(prefix="dfr-media-test-") as directory:
            source = Path(directory) / "source.mp4"
            output = Path(directory) / "cropped.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=3840x2176:r=24",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-frames:v", "9", "-t", "0.375", "-c:v", "libx264",
                "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-threads", "2", str(source),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=True, timeout=45)
            dfr._probe_video(source, 2176, 9)
            dfr._crop_video(source, output)
            dfr._probe_video(output, 2160, 9)
            hashes = []
            for path in (source, output):
                hashed = subprocess.run([
                    "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
                    "-map", "0:a:0", "-c:a", "copy", "-f", "hash",
                    "-hash", "sha256", "pipe:1",
                ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    check=True, timeout=15)
                hashes.append(hashed.stdout.strip())
            self.assertTrue(hashes[0].startswith(b"SHA256="))
            self.assertEqual(hashes[0], hashes[1])


if __name__ == "__main__":
    unittest.main()
