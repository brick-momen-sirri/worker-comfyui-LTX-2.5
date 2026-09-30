# Build, model inventory, and verification

Research and local checks were performed on 2026-09-10. This repository is an isolated working copy; the existing `D:\worker-comfyui` implementation was not edited.

## Reviewed source pins

| Component | Exact selection |
| --- | --- |
| Dockerfile frontend | docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e |
| CUDA base | `nvidia/cuda:13.0.2-base-ubuntu24.04@sha256:2ab6381d970b211fb93853796dc6707eb8a72575a375c422b17cf4d8b2641701` |
| OS packages | Ubuntu snapshot `20260910T000000Z`; CPython 3.12 from that snapshot |
| ComfyUI | `a7b1d39d342d102f305797fb5ba12dc304d9c1f5`, reporting v0.35.0 |
| Official LTX custom nodes | `15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d` |
| Original credit tracker | `8fbe08eb13d3875a5ca608e09131c29c4fd84386` (unchanged) |
| PyTorch / torchvision / torchaudio | `2.11.0` / `0.26.0` / `2.11.0`, CUDA 13.0 Linux wheels |
| Transformers / Diffusers / HF Hub | `4.57.6` / `0.36.0` / `0.36.2` |
| Kornia | `0.8.2`; required by the pinned LTX pyramid-blending imports |
| Runpod SDK | `1.7.13`, retaining the original `~=1.7.12` minor series |
| Dependency installer | `uv==0.10.0`, bootstrap wheel SHA256 locked |

`sources.lock.json` pins repository commits. `requirements/runtime.lock` pins all 162 resolved Python distributions and allowed artifact SHA256 values, including the CUDA dependency closure. `requirements/comfyui.in` and `requirements/ltx-nodes.in` are snapshots of the reviewed upstream requirement inputs. The Docker build uses the lock directly, then runs `pip check`; custom-node install failures are never ignored. CPU build smoke and startup also check that the preserved credit tracker registered its prompt and server-credit hooks, including all four required ComfyUI private API targets. It does not install ComfyUI-Manager or a runtime package installer workflow.

The first real CPU startup exposed a Kornia compatibility issue that dependency metadata alone did not catch: LTX's pyramid-blending node imports `pad` from Kornia's pyramid module, while Kornia 0.8.3 removed that alias. The lock now pins 0.8.2, which retains all required imports and satisfies ComfyUI's `>=0.7.1` requirement. The other 161 versions are unchanged. [Kornia 0.8.2 implementation](https://github.com/kornia/kornia/blob/v0.8.2/kornia/geometry/transform/pyramid.py), [0.8.3 implementation](https://github.com/kornia/kornia/blob/v0.8.3/kornia/geometry/transform/pyramid.py)

The pinned ComfyUI README requires cu130 or newer on modern NVIDIA GPUs; this is why this image advances beyond the original CUDA 12.8 worker. Use a Runpod host with a CUDA 13 compatible NVIDIA driver (R580 or newer). A real CUDA kernel runs in startup preflight before jobs are accepted. [Pinned ComfyUI installation guidance](https://github.com/comfyanonymous/ComfyUI/blob/a7b1d39d342d102f305797fb5ba12dc304d9c1f5/README.md)

## Complete bundled weights

The active machine-readable inventory is `models/manifest.json`; immutable profile inventories are `models/profiles/int8.json` and `models/profiles/bf16.json`. The readable table is `docs/model-inventory.md`. The default INT8 profile's eleven files total **44,542,597,655 bytes**, or **44.54 GB / 41.48 GiB**. The optional BF16 profile totals **75,947,642,823 bytes**, or **75.95 GB / 70.73 GiB**. Each file has an immutable Hugging Face repository revision, exact remote path, destination, byte count, and SHA256. Each normal Docker build downloads and verifies all eleven selected files before it can finish.

The complete INT8 image **built, exported, and loaded successfully**, with Docker build exit code **0** and local tag **`worker-comfyui-ltx25:int8`**. All eleven files, including the dedicated 2.5 pixel upscaler LoRA, passed exact size and SHA256 verification. The model stage reported `Verified 11 model files (44542597655 bytes)` and took **2,898.9 seconds (about 48 minutes 19 seconds)**, with every file downloaded on its first attempt. Docker reported **1,179.0 seconds** for export and **272.5 seconds** for unpacking. The optional BF16 transformer/encoder replacements were not downloaded, and the BF16 image remains unbuilt.

INT8 uses the publisher's **Comfy INT8 convrot** distilled transformer and Gemma4 encoder from the same pinned official LTX 2.5 repository. The selected ComfyUI version contains native loaders for this quantization format. The build changes the two loader filenames throughout the supplied API workflows and records the chosen profile. BF16 retains the original weight choices. This is weight quantization; the VAEs, latent upsampler, LoRAs and other tensors keep their original precision. No external quantization node pack or runtime conversion is required. [Pinned official model files](https://huggingface.co/Lightricks/LTX-2.5/tree/5e6e71018ee1756ed329b697a7b4aedc934dfce9)

Both standard profiles include the 2.5 DiffVAE video decoder, audio VAE with vocoder, latent spatial upscaler, the five 2.3 IC-LoRAs used by the supplied modes, and the dedicated 2.5 pixel spatial upscaler IC-LoRA. Their current catalog contains 21 modes: the original 18 plus three 4K presets. The 2.3 adapters are included specifically because the publisher's **2.5** example graphs reference those exact adapters; their presence is not an assumption that all older adapters work. [Pinned official 2.5 workflow directory](https://github.com/Lightricks/ComfyUI-LTXVideo/tree/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/example_workflows/2.5)

The native ComfyUI Gemma4 loader reads `tokenizer_json` directly from the text-encoder safetensors tensor data and constructs `tokenizers.Tokenizer.from_str`; no Google model checkout, separate tokenizer download, or runtime HF access is needed for these workflows. [Pinned tokenizer implementation](https://github.com/comfyanonymous/ComfyUI/blob/a7b1d39d342d102f305797fb5ba12dc304d9c1f5/comfy/text_encoders/gemma4.py)

Not selected: full/dev transformer, distilled LoRA for the dev transformer, NVFP4 alternatives, conv VAE alternative, duration predictor, optional prompt-enhancer encoder, temporal upscaler and optional full DFR pipeline. They are not dependencies of the supplied graphs. The model inventory deliberately matches executable workflows, not every optional file in the model family. Consult the separate workflow support documentation for supported and excluded modes. [Official model components](https://huggingface.co/Lightricks/LTX-2.5)

The added `video_upscale_x2` graph uses the official 2.5 pixel upscaler IC-LoRA at strength 1.0 and a reference at half the target width/height. It is creative 2x spatial upscaling, derived from the publisher's recommended IC-LoRA ComfyUI workflow; it synthesizes detail and does not perform frame interpolation. No additional temporal or decoder model is needed. Large output dimensions increase VRAM and RAM substantially. [Official 2.5 upscaler model card](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler)

Model access is gated on Hugging Face. Accept the repository access terms for each gated repository listed in the inventory using the account that owns the read token. Review the applicable model license before redistributing the resulting model-containing image. Store the image in a private registry unless your applicable license/access terms permit redistribution. No access grant is performed automatically.

Before downloading any missing weights, the downloader performs an authenticated **HEAD access check for every uncached file** and reports all denied repositories together. It checks the Hub's response without following or printing signed CDN redirects. Verified existing files are reused. Any denied, missing or failed required file prevents bulk downloads, so a missing adapter permission is discovered before transferring the transformer. All eleven access checks passed for the completed INT8 download after the user accepted the three previously missing adapter repository terms. See [test results](test-results.md) for the recorded access checks and resolved history.

## Hardware and storage planning

These are capacity estimates, **not measured generation benchmarks**:

| Resource | Default INT8 profile | Optional BF16 profile |
| --- | --- | --- |
| Build GPU / VRAM | None / 0 GB | None / 0 GB |
| Build host RAM | Approximately 16 GB allocated to Docker/WSL is a practical starting point; measured build requirements may differ | Same CPU/dependency build, larger downloads |
| Free build disk | Plan 150–200 GB for layers, cache, export/push and environment | Plan 250–300 GB |
| Initial generation GPU | RTX 6000 **Ada** 48 GB; verify CUDA 13 driver compatibility | A100/H100 80 GB or RTX PRO 6000 Blackwell 96 GB; verify driver |
| Initial generation RAM | 64 GB+; 96–128 GB gives more offloading headroom | 128 GB+; 192–256 GB for long/high-resolution editing |
| Startup VRAM gate | 23 GiB unless `LTX_MIN_VRAM_GB` overrides it | 47 GiB unless `LTX_MIN_VRAM_GB` overrides it |
| Deployed image storage | Plan 100 GB available for image extraction/storage | Plan 150 GB |
| Writable scratch | At least 30–50 GB per worker | At least 30–50 GB per worker |

The VRAM gates allow nominal 24 GB / 48 GB cards despite small differences in CUDA-visible capacity. They are admission policies, **not workload-fit guarantees**. The INT8 profile allows limited 24 GB tests; begin with a single-stage 512×288, 33-frame clip with other GPU workloads closed. Two-stage generation, DiffVAE decoding and upscaling can still run out of memory. No universal 16 GB support is claimed. The original Quadro RTX 6000 is a 24 GB card; select **RTX 6000 Ada 48 GB** when renting the suggested Runpod GPU.

The completed INT8 image has two measured size figures: Docker's **`Size` field reports 42,222,685,974 bytes (42.22 GB)**, while the running container's **apparent root filesystem size is 52,001,502,862 bytes (52.00 GB)**. These describe different storage views; neither is the total free space needed to build, cache, export, and run the image. The BF16 filesystem estimate remains approximately 90–105 GB and is unmeasured because that image has not been built. BuildKit downloads models into their final layer, without shipping a second HF cache copy. Registry export and Docker Desktop's expandable virtual disk can temporarily require more space than the final image. No network volume is required. Mount additional input/output storage when appropriate; do not hide `/comfyui/models` behind an empty volume.

The separate runtime-only image measured **7.45 GB apparent filesystem size** inside a container, while Docker's image-size field reported **3.93 GB**. The complete model image was subsequently built and measured as described above. The runtime build succeeded with this host's existing 8 GB WSL allocation; 16 GB remains a practical recommendation for more build headroom. Retain the **150–200 GB free build-space allowance**, plus **100 GB for deployed image storage and 30–50 GB writable scratch**, when planning a fresh INT8 build and deployment.

On the inspected Windows host, Docker Desktop's actual data VHD is on C:. C: had approximately **210 GB free before the build** and **52.9 GB free after the completed build**, including BuildKit/cache, the runtime image, and the full image. D: had **65.5 GB free at the post-build check**. The repository remains on D: and Docker data on C:. The host has 32 GB RAM, while Docker/WSL was allocated 8 GB RAM, 4 CPUs and 4 GB swap. Allocate around 16 GB RAM to Docker/WSL for a future build if the rest of the host permits it. Building is possible without freeing GPU memory; local generation needs free VRAM and can also be constrained by this host's 32 GB RAM. Closing applications or moving the workload to Runpod is safer than assuming quantization eliminates host-memory needs.

ComfyUI automatically offloads model components. `--cache-none`, tiled VAE nodes, and `COMFY_RESERVE_VRAM=2` reduce retained/intermediate memory but do not make arbitrary 4K or long-duration jobs fit. Two-stage generation doubles spatial dimensions and uses substantially more decoder and host memory. Editing and motion conditioning add guide tensors; outpainting increases canvas pixels. Set Runpod job execution deadlines only after GPU timing tests. The supplied worker handles one active generation at a time.

Native LoRA patching may temporarily dequantize weights, apply patches in floating point, and requantize them. That intermediate allocation can raise peak VRAM or host RAM beyond the stored INT8 file sizes, including for the dedicated upscaler LoRA. File bytes therefore cannot be used as a peak-VRAM estimate. Measure LoRA loading and first inference as well as steady-state generation when qualifying the target GPU.

The upstream custom-node README gives a generic 32 GB+ VRAM / 100 GB+ disk baseline for the broader LTX family. It does not certify every workflow or the experimental 24 GB test configuration above. Quantization reduces weight storage and can reduce resident VRAM; output quality, throughput, decoder memory and LoRA patching memory must be measured on the chosen GPU. [Publisher prerequisites](https://github.com/Lightricks/ComfyUI-LTXVideo/blob/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/README.md)

## Build and deployment commands

From PowerShell with Docker Desktop/BuildKit configured for Linux containers, the recommended helper handles Docker's PATH after a fresh install:

```powershell
Set-Location D:\worker-comfyui-LTX-2.5
# HF_TOKEN must already be set privately for a full model build.
.\scripts\build.ps1 -Profile int8 -Tag YOUR_REGISTRY/worker-comfyui-ltx25:int8-2026-09-10 -Push
```

Omit `-Push` to load the image locally, select `-Profile bf16` for BF16, or use `-HFTokenFile C:\secure\hf-token.txt` to pass an existing private token file as the secret. The model-free dependency check is `.\scripts\build.ps1 -Profile int8 -RuntimeOnly -Tag worker-comfyui-ltx25:runtime-check` and needs no HF token. All helper builds use `--platform linux/amd64`.

Direct Docker equivalent:

```powershell
Set-Location D:\worker-comfyui-LTX-2.5
# Set HF_TOKEN in this shell using your existing secret-management mechanism.
docker buildx build --platform linux/amd64 --build-arg MODEL_PROFILE=int8 --secret id=hf_token,env=HF_TOKEN --tag YOUR_REGISTRY/worker-comfyui-ltx25:int8-2026-09-10 --push .
```

INT8 is the default; `--build-arg MODEL_PROFILE=bf16` selects the larger bundle and rewrites the same graph loader bindings for BF16. Use a distinct image tag for each profile. Profile selection occurs at build time and is not a per-request option. Both normal builds are self-contained; setting an environment variable at startup cannot substitute missing weights.

If PowerShell was open before Docker was installed and `docker` is not on its PATH yet, use `scripts/build.ps1` or open a new terminal. The helper adds Docker's directory to PATH so its credential helper is available too. To keep a locally runnable image, replace `--push` with `--load` and use a local tag; `--push` uploads directly to the configured registry.

No token value is an image ARG or ENV. BuildKit mounts the token only during the model-download instruction; downloader redirects remove Authorization when crossing to the signed CDN host. The token is never logged or stored in a Hugging Face cache. Runtime has `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`; S3, Runpod job transport, and user-authorized partner nodes still use the network normally.

To check all required repository permissions using an existing private token file, without downloading model bodies or creating a model directory:

```powershell
python scripts/download_models.py --manifest models/manifest.json --root C:\ltx25-model-cache --token-file C:\secure\hf-token.txt --check-access-only
```

Replace the root with your existing model cache if one is available; cached files are hash-verified before being skipped. Use `--manifest models/profiles/bf16.json` to check the optional BF16 selection. The command never prints the token or signed redirect URLs. Do not put the private token file inside the repository or Docker build context.

A smaller **model-free build target** is available only to check imports and dependencies in CI. It is deliberately not a deployable worker and startup rejects its missing models:

```powershell
docker buildx build --platform linux/amd64 --target runtime --tag worker-comfyui-ltx25:runtime-check --load .
```

To run the full model image locally on a suitable Linux GPU host, use your private runtime environment file (keep it out of Git):

```bash
docker run --rm --gpus all --shm-size=16g \
  --env-file /secure/worker.env \
  -e SERVE_API_LOCALLY=true -p 127.0.0.1:8000:8000 \
  YOUR_REGISTRY/worker-comfyui-ltx25:int8-2026-09-10
```

Create a Runpod Serverless endpoint using the pushed **linux/amd64** image, one suitable GPU, one concurrent job per worker, appropriate RAM/disk, and the runtime secrets in the main README/API documentation. Preserve the existing `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, and `BUCKET_SECRET_ACCESS_KEY` values and any optional `BUCKET_REGION`; the legacy SDK determines the S3 bucket/key behavior. Do not provide HF credentials at runtime.

Startup honors `PUBLIC_KEY`, `SERVE_API_LOCALLY`, `COMFY_LOG_LEVEL`, the existing worker/credit environment, `COMFY_RESERVE_VRAM`, `COMFY_STARTUP_TIMEOUT`, `COMFY_EXTRA_ARGS` (whitespace-separated arguments; no shell evaluation), and `LTX_MIN_VRAM_GB`. ComfyUI listens on localhost; the local development Runpod API listens on port 8000. The supervisor exits if either process dies, forwards termination, and stops remaining processes after a bounded grace period. ComfyUI-Manager is absent, so it cannot trigger runtime installation/downloads.

## Verification actually performed

| Check | Observed result |
| --- | --- |
| Original Docker/start/integration inspection | Completed before edits; credit tracker source pin preserved |
| Official source checkout and source pins | Completed; exact commits and node definitions inspected |
| Model inventory | Eleven immutable INT8 HF revision/size/LFS SHA256 records checked against actual downloaded files; all exact sizes and SHA256 values passed. Optional BF16 replacement metadata is pinned but those two files were not downloaded |
| Ubuntu snapshot and base image digest | Snapshot Release endpoint HTTP 200; Docker Registry returned the pinned CUDA tag manifest/digest |
| Python dependency resolution | 162 packages resolved for CPython 3.12 Linux x86_64; a second resolution restricted to binary wheels also succeeded |
| Model downloader tests | 19 tests passed, including 11 new all-model access-preflight/progress cases; immutable revisions, path traversal rejection, corruption detection, token stripping, HTTPS-only redirect, atomic success/failure and verified-cache behavior |
| Real model access preflight | All eleven INT8 HEAD requests authorized on the latest check and in Docker. The user accepted the three initially missing adapter repository terms; those earlier HTTP 403 / GatedRepo responses are resolved |
| Startup shell | `bash -n src/start.sh` passed using Git Bash |
| Python syntax | `compileall` passed for scripts and the retained/adapted API download patch |
| API download compatibility patch | Applied successfully to the pinned current ComfyUI helper; repeat application was idempotent; redirect control preserved |
| Workflow/source validation | All generated modes and referenced model paths passed the source-schema validator; see the current workflow verification report for exact counts |
| Docker build environment | Docker Desktop successfully built and loaded the full `linux/amd64` INT8 image; build exit code 0 |
| CPU Docker import smoke | Passed as a mandatory build step, including pinned dependencies, LTX imports, workflow schemas, and preserved credit hooks |
| Full model download/hash verification | **Passed for all eleven INT8 files: 44,542,597,655 bytes**, including the dedicated 2.5 upscaler LoRA. Each file downloaded successfully on its first attempt; the stage took 2,898.9 seconds |
| Full model image export | **Passed**, local tag `worker-comfyui-ltx25:int8`; export 1,179.0 seconds, unpacking 272.5 seconds. Docker `Size`: 42,222,685,974 bytes; running-container apparent root filesystem: 52,001,502,862 bytes |
| Full worker startup on local RTX 4090 | **Passed**: all eleven baked files present with expected sizes, real CUDA kernel, all 18 workflow runtime schemas, 1,003 registered nodes, and preserved credit hooks |
| Local Runpod API and real media round trip | **Passed**: a 32×32 image supplied as a data URI ran through `LoadImage` → `SaveImage` and returned through the existing response contract. This exercised real ComfyUI execution and local result delivery, without LTX inference |
| GPU generation / real S3 delivery | **Not run** here; required Runpod acceptance tests remain |

The latest host capacity inspection is recorded above. The RTX 4090 has 24 GB VRAM, and the completed startup test confirmed CUDA access without stopping existing GPU processes. Full-image verification and the local image round trip passed. LTX inference, peak generation memory, live S3 delivery, and cloud callbacks remain untested; startup and schema checks do not establish those outcomes.

Before production traffic: deploy the built INT8 image to the target **RTX 6000 Ada 48 GB with at least 64 GB host RAM**; run short text, image, mixed-source first/last, video-to-video, audio, editing, and dedicated 2x upscaling fixtures; retrieve each delivered S3 result; verify codecs, frame count, duration/audio sync and callback/Runpod results; then test download failure, generation failure, upload failure, job timeout and a second sequential job. The build-time CPU smoke verifies imports/schema. The separate local startup test ran a real CUDA kernel, but no LTX model inference or generated-video quality assessment has been performed.
