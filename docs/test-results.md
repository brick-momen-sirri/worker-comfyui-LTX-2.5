# Validation report

## September 30 source publication checks

The local Windows checkout passed **150 worker tests** and **82 tester tests** before its initial GitHub publication. The checked-in 2560×1440 examples are validated with their documented CQ pixel and frame opt-ins; default admission remains restrictive. Source candidates were reviewed for credential patterns and large files. Credentials, model weights, research files, and generated media are excluded from Git. This publication did not rebuild or redeploy Docker.

The single 121-frame CQ job also completed at 2560×1440 and was presented at 24 FPS. Its measured timing, R2 delivery, full decoding, frame-count checks, and qualification limits are recorded in [single-job verification](cq-v2-2560-single121-verification.json).

## September 29 CQ Enhancer V2 profile

**Full HD regeneration also passed:** the original 960×540 bedroom clip was freshly processed through CQ V2 at **1920×1088**, using five overlapping jobs (33/49/49/49/49 frames). All returned `COMPLETED` with `success: true` and downloadable R2 video. Temporal trimming and four-row top/bottom cropping produced **1920×1080, 200 frames, 30 FPS, 6.666667 seconds**; there was no spatial resize after AI inference. Full decoding and exact dimensions/frame counts passed for every source result and the final assembly. Sampled first/middle/last and join-adjacent frames were visually inspected. The original image was reused through its existing workflow API; updated named-mode caps are local source changes, not a newly deployed image. See [job IDs and verification](cq-v2-1920-verification.json). Full HD 153-frame inference, exact GPU identity and peak VRAM remain unmeasured. The admission/compatibility changes passed 62 targeted tests; the example contract and JavaScript/Python syntax checks also passed.

**First Runpod CQ sample passed:** job `8520dc98-2010-4fdf-864b-502ac2a1f1f3-e2` completed on endpoint `dfadob3rm5dg32`, worker `4ihr6o8sbu3t1f`, with **50.481 seconds execution** and **798.243 seconds queue time**. Its 320×176 base64 source produced a **1280×704, 33-frame, 30 FPS, 1.1-second H.264 video** with 48 kHz stereo AAC audio of the same duration. The worker reported `success: true` and returned an R2 URL; the 1,880,096-byte result downloaded successfully, passed full FFmpeg decoding and frame counting, and loaded in the browser with `readyState=4` and no media error. SHA256: `0dfc28786e0b41acbbcb39f4c47e21ff19c1365985b9d623c29c01a33ea05324`. First/middle/last-frame inspection and a side-by-side comparison showed sharper detail with changes in color, lighting and fine structures. This was a qualitative inspection, not a fidelity or temporal-quality benchmark. Source: the [Big Buck Bunny sample hosted by W3Schools](https://www.w3schools.com/html/html5_video.asp), copyright 2008 Blender Foundation. Evidence and media are in `.research/cq-v2-runpod-test/`.

The initial queue delay came from a GPU configuration mismatch. September 29 logs from workers `b4ng7myb3thjb9` and `x9ilzar681bhqy` show all six model files verified by size, followed by startup exit code 1: **31.4 GiB total VRAM** was assigned, below the CQ admission floor of **47 GiB**. After the operator updated GPU settings, the existing job completed without rebuilding the image or submitting another job. Aggregate health counts had reported ready/idle workers despite startup failures, so those counts alone did not establish CQ readiness. The completed job response does not identify the GPU model or report total/peak VRAM; those measurements remain unknown.

The complete CQ image was pushed successfully to the private Docker Hub repository as `momensirribrick/worker-comfyui-ltx25-cq:cq-v2` on September 29. Push exit code was 0. Authenticated remote inspection returned index digest `sha256:9bd09e9b9ee2386feb0e09d138be8a761e5faf61512897ea9140c667617b3c8a`, matching the local build, and Linux/AMD64 manifest `sha256:ddca390b85e905a0c5a3f906fdf805f53d7053fd7519d206714a7b5f70230b24`. Anonymous repository access returned HTTP 404 before and after upload. Logs are `.research/cq-v2-push.log` and `.research/cq-v2-remote-manifest.log`. This verifies registry publication; it does not establish Runpod generation success.

The separate `cq-v2` ComfyUI profile passed **150 worker tests** and **75 tester tests** after the final source changes. Python compilation and the tester JavaScript syntax check also passed. Its graph also passed the [pinned-source schema check](cq-v2-workflow-validation.json). The `linux/amd64` model-free runtime target built with exit code 0. During that build ComfyUI 0.35.0 registered **1,003 nodes**, and the live `/object_info` validation accepted the one CQ API graph with **22 unique node classes**, all six referenced model paths, and no schema errors. No weights were loaded and no generation ran in this build target.

The six-file inventory is **48,934,613,516 bytes / 45.57 GiB**. Hugging Face access passed for all six files. The complete `linux/amd64` image built with exit code 0 as `momensirribrick/worker-comfyui-ltx25:cq-v2`; every model passed its pinned download SHA256 and the final image recheck found all six expected sizes. Docker reports image ID `sha256:9bd09e9b9ee2386feb0e09d138be8a761e5faf61512897ea9140c667617b3c8a`, `image inspect` size **45,893,445,282 bytes**, apparent root filesystem size **56,567,128,064 bytes**, local image storage **63.88 GB**, and profile label `cq-v2`. Docker's list view reports a 102 GB virtual size, so capacity planning uses the measured local storage plus build cache rather than that virtual number alone. The token value is absent from the image configuration/history, expected credential variables are unset, and token files are absent.

The full image started healthy on the local RTX 4090 after setting `LTX_MIN_VRAM_GB=0` for startup validation only. Its normal 47 GiB admission policy correctly rejected the 24.0 GiB card. With the local override, CUDA 13, a CUDA kernel, native BF16, 1,003 nodes, workflow validation, and `/runsync` missing-video rejection all passed. No LTX weights were loaded and no generation ran in that local startup test; the subsequent real CQ inference and R2 delivery are recorded above. Full 153-frame inference, peak VRAM and exact visual equivalence to the publisher's original GUI workflow remain untested. [Machine-readable CQ evidence](cq-v2-verification.json) and [CQ build/API guide](cq-v2.md).

## September 12 update

Before the CQ profile was added, that source revision passed **147 worker tests and 73 tester tests**. The counts in the individual qualification runs below record their earlier source revisions.

The user-authorized local Docker cleanup on September 12 removed the local Comfy INT8 image to make room for the DFR build. Its successful build and test evidence remains valid; `momensirribrick/worker-comfyui-ltx25:int8` remains in the registry and the existing Runpod deployment is unaffected. Pull that registry tag before repeating local checks or retagging it for publication.

**121-frame native follow-up:** job `a69e86cc-1ff6-405c-a82f-707426c1dc1d-e2` completed successfully in **1041.038 seconds**, with **13.498 seconds** queue time and an S3 video that loaded at **3840×2160 / 5.041667 seconds**. Only the frame count changed from the preceding direct 4K nine-frame test; the default graph hash is unchanged. Eight native graph tests and all 62 tester tests passed after the admission and request-timeout changes. Peak VRAM was not measured. [Full native evidence](native-4k-verification.json)

**Direct 4K follow-up:** `image_to_video_native_4k` completed a real nine-frame job on the same deployed INT8 worker in **73.208 seconds**, returning an S3 video that loaded at **3840×2160 / 0.375 seconds**. This graph samples once at 3840×2176 and crops to UHD, with no output upscaler. At that checkpoint, the source passed **119 worker tests, 62 tester tests, and pinned-source schema checks for 21 workflows / 55 classes**. See [direct 4K evidence and remaining limits](native-4k.md). The staged-upscaling checks below were the preceding qualification run.

The updated source passed **111 worker tests** and **61 local tester tests**. Pinned-source schema validation passed for **20 workflows and 55 node classes**. The original deployed INT8 image generated and delivered a 1280×768, 121-frame clip, followed by exact **3840×2160 image-to-video clips of 9 and 33 frames at 24 fps**. Both 4K results were delivered through S3 and loaded in the browser. The 33-frame clip lasts 1.375 seconds and took 266.635 seconds of worker execution. These are real Runpod results, distinct from the earlier local fixtures and mocked tests.

See [4K verification](4k-verification.json) and [the 4K guide](4k.md) for job identifiers, timing, hardware scope, and limitations. The separate 4K video-upscale mode and updated native named API have not been GPU tested. The new Comfy Docker application layer has not been built or pushed; Docker Desktop could not start during that qualification run and has since recovered. The deployed original image executes the new bundled graphs through the tester's compatibility transport. Peak VRAM and callback/webhook behavior were not measured.

## September 10 original image verification

The implementation is in the isolated `D:\worker-comfyui-LTX-2.5` working copy on branch `ltx-2.5-production`. The original `D:\worker-comfyui` checkout remains at `114da7fa3ceaa7113468ac43ed7f4f53c71674f3` with its original modified Dockerfile/docker-bake files and untracked local files unchanged.

The tracked [image verification record](image-verification.json) contains the measured full image ID/sizes, successful build/startup checks, and actual local API request results.

| Check actually run | Result and scope |
|---|---|
| `python -m unittest discover -s tests -q` | **103 tests passed**, 1.856 seconds on the final recorded run; zero skipped tests |
| Same suite inside Docker/Linux | **103 tests passed**, 3.982 seconds, zero skipped tests. Repository mounted read-only into the built runtime image; real Linux Pillow/FFmpeg exercised, ComfyUI execution and S3 delivery mocked |
| Media decoding | Real Pillow and FFmpeg/FFprobe tests: raw base64, data URI, normalized PNG/H.264/AAC/WAV, exact frame count, short-video failure, audio-stream validation, silent-video audio insertion |
| Download handling | Mocked HTTPS streams and DNS: byte caps, truncated response, 403, strict HTTPS, forbidden credentials/ports, private/mixed DNS, redirect rejection and URL-query error redaction |
| All named modes | **18** manifests compiled through the adapter with mocked generation; media namespacing, parameter/seed/dimension bindings, 2× upscale factors, output-prefix isolation |
| Delivery/integration | Legacy tests plus added cases: original job ID, S3 MIME types/URL validation, source-preview filtering, duplicate output handling, failed/partial uploads, execution failures, timeout/recycling, credit results, cleanup ordering and inline response limits; ComfyUI and S3 mocked |
| Model downloader | **19 tests passed**, including 11 new access-preflight/progress cases; immutable revision/path/hash inventory, corruption rejection, atomic success/failure, cross-host credential stripping, HTTPS redirects, all-model access checks before bulk downloads, safe redirect handling, progress reporting and verified-cache behavior. Synthetic files, no real model weights |
| Profile selection | INT8 and BF16 inventories and workflow selection passed; only the transformer and text-encoder filenames change. The DiffVAE and all six LoRAs remain identical; invalid profiles fail clearly |
| Source-schema check | **Both INT8 and BF16** passed: **18 workflows**, **54 exact node classes**, **11 referenced model paths** each; no missing required inputs, bad output slots, cycles, broken bindings, or model-inventory mismatches found. [INT8 report](workflow-validation.json), [BF16 report](../workflows/static-validation-bf16.json) |
| Dynamic node inputs | Isolated execution of pinned ComfyUI schema constructors and DynamicCombo expansion passed for all 18 output nodes; MP4/H.264 and FLAC settings checked. [Report](dynamic-input-validation.json) |
| Dependency compatibility | All 162 hash-locked distributions installed in Linux CPython 3.12; `pip check` passed. A real import failure with Kornia 0.8.3 was found and fixed by pinning compatible 0.8.2; the other 161 versions remain unchanged. [Details](build-research.md) |
| ComfyUI CPU startup | **Passed: 1,003 nodes registered**, including official LTX nodes and credit tracker. All four preserved credit hooks and the 18 API workflow schemas passed against the running server. Hugging Face offline mode enabled; no model weights loaded and no generation executed |
| Source and build pins | Official source commits, model revision/size/LFS SHA256 metadata, Docker base digest, and Ubuntu snapshot were checked; all eleven actual INT8 model downloads passed their exact size and SHA256 checks |
| Real Hugging Face access preflight | **All eleven INT8 files authorized** on the latest authenticated HEAD recheck and in the Docker build. The user accepted the three previously missing repository terms; the earlier Ingredients, In-Outpainting and Deblur HTTP 403 / GatedRepo responses are resolved |
| Docker build | Docker **28.1.1**, Desktop **4.41.2**, Linux/amd64: **full INT8 image built and loaded successfully**, exit code **0**, as `worker-comfyui-ltx25:int8`. All eleven model downloads passed exact size/SHA256 verification. The separate model-free runtime target also built successfully |
| Full-image startup | **Passed** through the actual `start.sh`: eleven model sizes verified, real RTX 4090 CUDA kernel passed, 1,003 nodes registered, all 18 workflows / 54 referenced node classes / eleven model filenames validated, preserved credit hooks registered, and the local Runpod SDK API became ready. `scripts/healthcheck.py` exited **0**; container health was `healthy` and `OOMKilled=false` |
| Full-image local API smoke | **Passed in 0.578 seconds**: actual `/runsync` rejected missing image input as `FAILED` with expected error and job ID; a real `LoadImage` → `SaveImage` graph accepted a 32×32 base64 data URI and returned `COMPLETED`, preserving the pixel value, job ID, `success`, `prompt_id`, `credit_usage` and inline base64 output. No LTX inference or S3 configuration |
| Runtime credential check | Confirmed `HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN` and S3 credential environment variables absent; `/run/secrets/hf_token` and both usual Hugging Face token-file locations absent |
| Real worker imports | Built image imported `worker` and the actual Runpod SDK successfully: Runpod 1.7.13, PyTorch 2.11.0, Kornia 0.8.2, comfy-kitchen 0.2.33 |
| CUDA/Docker preflight | **Passed** with `docker run --gpus all`: real CUDA allocation/arithmetic/synchronization and native BF16 support check on RTX 4090; PyTorch **2.11.0+cu130**, CUDA **13.0**, INT8 admission policy. No LTX inference |
| Missing-model startup | Runtime-only image correctly exited with code 1 and listed all missing weights before starting ComfyUI or accepting jobs |
| Source patch | Preserved Comfy API media-download timeout patch applied to pinned ComfyUI source, with idempotent repeat check |
| Syntax | Python compilation passed; startup shell passed `bash -n` using Git Bash |
| SDK contract | Exact hash-matching Runpod 1.7.13 PyPI wheel inspected for month-bucket naming, job-ID prefix, upload URL behavior and SDK completion/error handling |

Reproduce the source-based checks:

```powershell
python -m unittest discover -s tests -q
python scripts/validate_workflows.py --comfy-source .research/comfyui --ltx-source .research/ltx --report docs/workflow-validation.json
python scripts/check_dynamic_inputs.py --comfy-source .research/comfyui --report docs/dynamic-input-validation.json
python -m compileall -q handler.py worker.py ltx_worker scripts tests examples
```

The `.research` directories are ignored inspection checkouts of the exact commits listed in `sources.lock.json`. Use the corresponding pinned checkouts when reproducing source checks elsewhere. Reports explicitly set `generation_executed: false` and do not imply full ComfyUI imports.

Repeat the local startup/API smoke by pulling the published INT8 image, exposing the development API only on loopback and passing no S3 settings. This restores image layers removed during local cleanup and needs sufficient Docker disk space:

```powershell
Set-Location D:\worker-comfyui-LTX-2.5
docker pull momensirribrick/worker-comfyui-ltx25:int8
docker run --detach --rm --name ltx25-local-smoke --gpus all --shm-size=16g -e SERVE_API_LOCALLY=true -e ALLOW_CUSTOM_WORKFLOWS=true -e RUNPOD_POD_ID=ltx-local-smoke -p 127.0.0.1:8000:8000 momensirribrick/worker-comfyui-ltx25:int8
python examples/smoke_local_worker.py --url http://127.0.0.1:8000 --report artifacts/local-worker-smoke.json
docker exec ltx25-local-smoke python /scripts/healthcheck.py
docker stop ltx25-local-smoke
```

The helper waits for API readiness before sending its two requests. This tests validation and legacy image transport through the actual worker and ComfyUI; it does not load LTX weights for inference. The local SDK's failed `/runsync` response omits `output.error_code`; the smoke checks its actual error/status/job-ID contract. The report is retained in ignored `artifacts/local-worker-smoke.json`. Keep `ALLOW_CUSTOM_WORKFLOWS=false` for a production endpoint that only accepts named modes.

## Build progress and remaining verification

The following measurements and test limits describe the September 10 local build. Subsequent Runpod tests and September 12 local-image cleanup are recorded above.

- **Runtime image built:** `worker-comfyui-ltx25:runtime-check`, Linux/amd64. Local image ID: `sha256:e5e01779bb1338cd94413744b45f0f29662fa5032f32880f8258b40e10ed5234`. Docker reported size 3,930,563,250 bytes; an ephemeral container's apparent root filesystem measured 7,451,770,889 bytes. Neither is the full model image size. No registry push was performed.
- **Full INT8 image built and loaded:** `worker-comfyui-ltx25:int8`, Linux/amd64, build exit code **0**. Local image ID: `sha256:74f62ddb4caddb1ec791c90f1d74c59dd84092b07cb507d290d3b1aa9c9905fa`. Docker's engine-reported `Size` is **42,222,685,974 bytes (42.22 GB)**; the running full container's root filesystem measured **52,001,502,862 apparent bytes (52.00 GB / 48.43 GiB)** with `du --apparent-size -sx -B1 /`. These are different measurements. The model download/hash stage took **2,898.9 seconds**, layer export **1,179.0 seconds**, and unpack **272.5 seconds**. No registry push was performed.
- **Full ComfyUI CPU import smoke:** passed as a mandatory Docker build step, including LTX registration, preserved credit hooks, and all 18 graph schemas. The first attempt exposed Kornia 0.8.3's removed import; pinning 0.8.2 fixed the failure without changing LTX source.
- **Full model download/SHA256 verification:** **passed for all eleven INT8 files**, totaling **44,542,597,655 bytes**, including the dedicated LTX 2.5 pixel upscaler LoRA. Every file succeeded on its first download attempt. The completed build reported `Verified 11 model files (44542597655 bytes)`.
- **Final full-image startup/API smoke:** **passed**, including actual worker startup, health check, invalid-input failure and the completed image round trip described above. Successful file verification, startup and transport do not establish LTX inference or generated-video quality.
- **GPU inference:** the CUDA kernel preflight passed, but no LTX generation or memory-fit validation has run. Windows `nvidia-smi` reported 24,564 MiB total, 19,529 MiB used and 4,614 MiB free after the test; WSL/PyTorch reported 21.8 GiB free during preflight. Treat the differing memory reports conservatively; the preflight's free-memory value is informational. No existing GPU processes were stopped.
- **Live output S3 upload, Runpod completed/failed envelopes and webhooks:** tested at the contract/mock level only. No production request was submitted.
- **Quality/VRAM/speed claims:** none measured. In particular the special 2.5 pixel upscaler has not generated an observed result on this machine.

The healthy smoke container was stopped and removed successfully after response verification. The INT8 image was subsequently published and deployed to Runpod, then removed locally during the authorized September 12 cleanup. The [README upload commands](../README.md#build-and-deploy) now pull the registry copy before retagging and pushing it; they do not depend on the removed local tag.

At the September 10 build, Docker Desktop's data VHD was on **C:**, which had approximately **210 GB free** before building and **52.9 GB free** afterward. No Docker images or build caches had been pruned at that point; the authorized cleanup occurred on September 12. The runtime build succeeded with **8 GB WSL RAM**, **4 CPUs** and **4 GB swap** configured; the host has **32 GB RAM**. Peak memory was not instrumented. Building uses no VRAM; the later CUDA preflight briefly used the GPU. See [build research](build-research.md) for full-image capacity planning and the recommended WSL allocation.

Actual build/check logs are retained locally under the ignored `.research` directory: `docker-runtime-int8-fixed.log`, `docker-linux-tests.log`, `docker-worker-import.log`, `docker-cuda-preflight.log`, and `docker-missing-models.log`. The **successful full build** is in `docker-full-int8-authorized.log`; the **successful full-image startup** is in `docker-full-int8-startup.log`. `docker-full-int8-build.log` records the earlier failed access-preflight attempt before the user accepted the remaining repository terms. Test log messages about successful S3 uploads come from mocked tests; they do not record a live S3 upload.

**Resolved access issue:** the first preflight authorized eight files and rejected Ingredients, In-Outpainting and Deblur. After the user accepted those repositories' terms, a real recheck and the full Docker build authorized all eleven files. The all-model preflight prevented bulk downloads during the earlier denied attempt; all eleven downloads and hash checks subsequently completed successfully.

These exact **2.3 IC-LoRAs** are referenced by the publisher's pinned **2.5 workflows**, so they remain part of this LTX 2.5 worker. The dedicated pixel upscaler uses the separate **2.5 Pixel Spatial Upscaler LoRA**. [Pinned official 2.5 workflows](https://github.com/Lightricks/ComfyUI-LTXVideo/tree/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/example_workflows/2.5), [official 2.5 upscaler](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler)

## Runpod acceptance required before production

1. The local full INT8 build, CPU import/credit checks and all eleven weight hash checks passed; the historical local image ID and engine-reported size are recorded above. The image is already published and deployed. For another deployment, use the registry copy and record its immutable digest; the removed local tag is not required.
2. For INT8, start on a CUDA 13 compatible **RTX 6000 Ada 48 GB** with **64 GB+ system RAM** and the documented disk allowance. BF16's initial recommendation remains an 80 GB GPU with 128 GB+ RAM. Confirm model size verification, real CUDA kernel preflight, registered node/schema checks and worker readiness; these hardware choices are test starting points, not universal memory-fit guarantees.
3. Run short 9-frame T2V, I2V, first/last mixed base64+presigned URL, V2V, audio-guided and text-to-audio requests. Start small before 121/241-frame or two-stage workloads.
4. Run `examples/video_upscale_x2_s3.json` after supplying a valid URL, or generate a base64 payload with the helper. Confirm a 512×288 source produces 1024×576; verify frame count, audio sync and visual detail. Include a silent input.
5. Download every delivered S3 URL, verify its content and MIME type, and check that the job ID, `prompt_id`, media arrays and credit fields match the original caller's expectations. Confirm Runpod invokes the existing webhook once per its own delivery policy.
6. Test expired source URL, invalid media, a denied S3 upload, generation failure and timeout. Each must fail clearly; partial artifacts must not make the job successful. Confirm required uploads finish before completion and that failed workers recycle.
7. Run two sequential successful jobs and check for input/output cross-contamination, memory growth, temporary-file accumulation and credit tracking. Measure peak VRAM/RAM, latency and cold-start time at each planned production preset.

The full INT8 image, model hashes, startup and local image transport were verified, followed by the Runpod generation and S3 delivery checks recorded above. Production readiness still requires qualification of the remaining modes, visual quality, resource use, failure handling, and cloud lifecycle behavior.
