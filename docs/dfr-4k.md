# Official DFR 4K worker

This is a separate Runpod worker image using Lightricks' actual Python
`ltx_pipelines.dfr_pipeline`, pinned at
`a95ab856bf29407b6b066ede0abe1846050db56c`. It does not approximate DFR with the
existing ComfyUI upscale graph. The existing INT8 image and endpoint remain usable.

The official recipe generates at **960×544 → 1920×1088 → 3840×2176** with
`spatial_upscalings=2`. Upstream handles generated keyframes, reference conditioning,
the 0.5 detailing LoRA, spatial blending between denoising steps, and keyframe-aware
DiffVAE decoding. The wrapper leaves those algorithms and automatic tiling intact.
Temporal upscaling stays zero: 121 frames at 24 fps is approximately 5.04 seconds.
After generation, FFmpeg crops eight rows from each edge to deliver **3840×2160**;
this delivery crop is our addition. [Official DFR documentation](https://github.com/Lightricks/LTX-2/blob/a95ab856bf29407b6b066ede0abe1846050db56c/packages/ltx-pipelines/docs/pipelines.md#12-dfrpipeline)

## Models and runtime

The complete manifest is `models/profiles/dfr.json`: exact repository revisions,
filenames, byte sizes, and SHA256 hashes. All six files download during the final
Docker build and are checked again at startup. No startup download or HF login runs.

| File under `/models/ltx25` | Decimal GB |
| --- | ---: |
| `diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors` | 42.018 |
| `text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors` | 26.264 |
| `vae/ltx-2.5-video-vae-bf16.safetensors` | 1.472 |
| `vae/ltx-2.5-audio-vae-bf16.safetensors` | 0.365 |
| `latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` | 0.996 |
| `loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors` | 0.327 |

Total: **71,442,240,044 bytes (66.54 GiB)**. The transformer and text encoder differ
from the current Comfy INT8 files and account for 68.28 GB. The remaining four assets
are the same compatible files; this standalone Dockerfile downloads its own complete
bundle. Gemma configuration/tokenizer and the audio vocoder are embedded; temporal
upsampling and prompt enhancement are disabled, so their extra assets are unnecessary.
Python DFR cannot load the Comfy ConvRot INT8 checkpoints. [Official model card](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/README.md)

Dependencies are separately hash-locked in `requirements/dfr.lock`: PyTorch 2.13
CUDA 13.2, NATTEN 0.21.7, and compatible Transformers. The unusual torchaudio
2.11.0+cu132 wheel follows upstream's explicit test index; its internal runtime
version string reports 2.11.0+cu130. Build checks include real CPU resampling and
vision operations; CUDA audio compatibility still requires GPU verification. Upstream requires a
cuDNN 9.24.0.43 override despite torch metadata asking for 9.20.0.48; the build
smoke accepts only that documented pip-check discrepancy. It rejects all others.
The CUDA 13.0 base supplies OS/driver integration; these Python wheels supply the
CUDA 13.2 libraries. No packages are installed into the ComfyUI environment.
[Official dependency configuration](https://github.com/Lightricks/LTX-2/blob/a95ab856bf29407b6b066ede0abe1846050db56c/pyproject.toml)

## Hardware and storage

- Target: **48 GB RTX 6000 Ada**, one job per worker, BF16 with official CPU offload.
  Admission requires at least 44 GiB VRAM. That threshold is a deployment policy,
  not a measured guarantee of fit. DiffVAE decoding remains a possible memory limit.
- Plan **128 GB host RAM**. CPU offload retains substantial weights in RAM.
- Plan **220–250 GB of additional free capacity on the Docker data drive** for a
  conventional full build with `unpack=true`, beyond existing images and cache.
  The earlier 150 GB estimate was insufficient for a safe unpacking peak: the
  71.44 GB model build snapshot, roughly 56 GB compressed model layer, a second
  unpacked model copy, and runtime layers can coexist. The revised range is a
  planning estimate based on that observed duplication, not the final image size.
  Use at least **120 GB container disk** on Runpod for models, runtime, and scratch.
- This profile requires **Linux NVIDIA driver ≥595.45.04** for the CUDA 13.2/JIT
  stack. CUDA minor compatibility on older drivers does not prove support for
  newer PTX. The startup preflight rejects an older host before accepting jobs.
  [NVIDIA compatibility rules](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html)

Docker Desktop stores Linux image data inside its VHD. Space reclaimed **inside
that VHD** can be reused by Docker even when Windows reports no increase in free
space on C:. It does not mean the VHD has shrunk. Conversely, the large virtual
capacity shown by Linux `df` is not extra physical storage: the VHD can grow only
while its Windows drive has room. Plan for both reusable space already allocated
inside the VHD and any additional growth on the Windows drive. Inspect Windows
free space with `Get-PSDrive C,D`; `docker system df` reports Docker image/cache
usage but does not establish how much disk space Windows has recovered. The build
helper does not prune or compact Docker storage.

## Build and deploy

On this Windows host, use native PowerShell and the Docker Desktop CLI. This path
is working; Ubuntu's WSL Docker socket is currently disconnected. From the project:

```powershell
Set-Location D:\worker-comfyui-LTX-2.5
.\scripts\build-dfr.ps1 -HFTokenFile 'C:\private\hf-token.txt'
```

Replace the token-file example with your private file, or omit `-HFTokenFile` when
`HF_TOKEN` is already set privately in the PowerShell environment. The helper finds
Docker Desktop even when the current shell's PATH has not refreshed. The token is
a BuildKit secret, never a build ARG.

The helper uses
`--output type=image,compression=gzip,compression-level=1,unpack=true` for local
image import. The full DFR build completed all six model size/SHA256 checks and
image export; final unpacking and offline verification are separate steps.
Gzip level 1 limits compression work on newly exported layers; cached/base layers
can retain their existing compression.
`unpack=true` uses Docker Desktop's containerd image store.
[Docker image exporter options](https://docs.docker.com/build/exporters/image-registry/)

**Low-space recovery after a completed export:** on September 12, image export
finished in 750 seconds, but unpacking reduced C: free space to 17.55 GiB. Only
the task's build CLI was canceled. The exported tag was preserved, and after
the build snapshot was released Docker's used space measured
96.37 GB; a subsequent targeted cache prune returned zero bytes because the
snapshot had already been released. This did not itself prove a successful import
or worker startup. Before rebuilding after such an interruption, check
`docker image inspect momensirribrick/worker-ltx25-dfr:4k-v1`. When the completed
image is present and adequate space is available, starting its verification
container with `--pull never` lets Docker unpack the stored layers without another
model download. This recovery applies only after a confirmed complete export;
canceling earlier may leave no usable image. Unpacking and verification results
are tracked in the Verification section below.

After a successful full build, publish the existing local image without rebuilding:

```powershell
$dockerExe = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
& $dockerExe image inspect momensirribrick/worker-ltx25-dfr:4k-v1
& $dockerExe login
& $dockerExe push momensirribrick/worker-ltx25-dfr:4k-v1
```

Alternatively, add `-Push` to the helper to build and publish in one operation.
It keeps the same gzip/export settings and adds `push=true`; log in first. The
runtime-only target cannot be pushed through the helper. Pin the resulting
registry digest in the Runpod template after a successful push.

For a Linux shell with a working connection to Docker and `HF_TOKEN` already set,
the equivalent build command is:

```bash
cd /mnt/d/worker-comfyui-LTX-2.5
docker buildx build --platform linux/amd64 -f Dockerfile.dfr \
  --secret id=hf_token,env=HF_TOKEN \
  -t momensirribrick/worker-ltx25-dfr:4k-v1 \
  --output 'type=image,compression=gzip,compression-level=1,unpack=true' .
```

Use a separate Serverless endpoint for this image while qualifying it. Configure
the **same** `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, and
`BUCKET_SECRET_ACCESS_KEY` secrets as the working endpoint. The original uploader's
bucket convention, job-ID prefix, MIME type, and signed response URL are preserved.
Completion is returned only after generation, file verification, crop verification,
and required S3 upload. Runpod remains responsible for outer webhooks and job status.

Runtime settings:

| Variable | Default / purpose |
| --- | --- |
| `DFR_MODEL_ROOT` | `/models/ltx25`; keep the baked bundle path unless mounting the identical complete bundle |
| `DFR_EXECUTION_TIMEOUT_S` | `3600`, pipeline subprocess limit |
| `MEDIA_PROCESS_TIMEOUT_S` | `180`, per FFprobe / FFmpeg operation |
| `MAX_DFR_OUTPUT_BYTES` | `536870912`, each local output limit |
| `MAX_IMAGE_BYTES` | `20971520`, same bounded image ingest as Comfy worker |
| `INPUT_ALLOWED_HOSTS` | Optional exact hostname allowlist |
| `INPUT_DOWNLOAD_TIMEOUT_S` | `300` |
| `MAX_INLINE_OUTPUT_BYTES` | `5242880`; use S3 for 4K |
| `MAX_RESULT_BYTES` | `8388608` |
| `SERVE_API_LOCALLY` | `false`; optional Runpod SDK local development API |

The caller's request policy should allow 90 minutes (`5400000` ms) with a two-hour
TTL (`7200000` ms), including verification/upload time beyond the pipeline limit.
Configure the endpoint's timeout accordingly. Peak VRAM and execution time must be
measured on the selected host; increasing a timeout does not solve an out-of-memory error.

## API and tester

Modes: `image_to_video_dfr_4k` and `text_to_video_dfr_4k`. Use a nonempty `prompt`;
I2V additionally needs `media.image` as base64, a data URI, or an HTTPS/S3 URL.
Parameters: `num_frames` **9, 33, or 121** (default 33), `seed` from 0 to 4294967295,
and I2V `image_strength` from 0 to 1 (default 0.8). Width 3840, height 2176, and
fps 24 are fixed. The limited frame choices are our initial qualification profile,
not a general model limit. Nine requested frames still use an internally padded
25-frame canvas before upstream trims the output.

Examples are in `examples/image_to_video_dfr_4k.json` and
`examples/text_to_video_dfr_4k.json`; `examples/image_to_video_dfr_4k_base64.json`
demonstrates a data URI with a small sample image. The `videos` result contract stays the same.
`prompt_id` is an execution UUID, explicitly identified in `backend.prompt_id_kind`;
there is no ComfyUI prompt in this runtime. Comfy credit fields report no paid nodes.
Failures return `success:false`, `error`, and `error_code`, with no video marked delivered.

Illustrative worker output (not evidence of a GPU run):

```json
{"success":true,"job_id":"original-runpod-id","prompt_id":"execution-uuid",
 "backend":{"name":"ltx_pipelines.dfr_pipeline","prompt_id_kind":"worker_execution_uuid"},
 "videos":[{"filename":"LTX25-DFR.mp4","media_type":"video","format":"video/mp4",
 "frame_rate":24,"type":"s3_url","data":"https://storage.example/result.mp4?presigned-query"}]}
```

The real response also includes the existing `comfy_credits` and `credit_usage` fields.
For an upload failure it returns `success:false`, `error_code:"OUTPUT_UPLOAD_FAILED"`,
and `error:"Required S3 upload failed"`. Invalid media and settings use the shared
input error codes; GPU memory failures use `GPU_OUT_OF_MEMORY`. The outer Runpod
envelope still identifies the original job.

Start a separate tester after deploying the DFR image:

```bash
python tools/runpod_tester/server.py --runtime dfr \
  --endpoint YOUR_DFR_ENDPOINT_ID --port 8767 --keep-key-in-memory
```

Open `http://127.0.0.1:8767/`, enter the API key and S3 upload settings, select
image-to-video DFR, and upload the same source. First qualify 33 frames, inspect
color and temporal consistency, then test 121. This uses named DFR requests only;
it cannot run on the old INT8 endpoint through the legacy workflow bridge.

## Verification

On September 12 the complete image was exported, loaded, and verified as
`momensirribrick/worker-ltx25-dfr:4k-v1`, local image ID
`sha256:a8be5da29781dd2e6f4338019f6a8d05efa18ace2ac2277b50aa623a65a7c908`.
Docker reports **59,687,474,540 bytes** of packaged image data; its container
filesystem measures **78,508,413,545 apparent bytes**. Docker's image listing shows
138 GB including packaged and unpacked content. These are different measurements.
The image has **not been pushed**.

All **six model downloads passed size/SHA256 verification**. A second full SHA256
check of all **71,442,240,044 model bytes inside the completed image**, with networking
disabled, passed in 61 seconds. Offline CPU checks also passed: official split-model
CLI parsing, generated-keyframe configuration, DiffVAE recognition, audio decoder
and vocoder keys, embedded Gemma4 tokenizer/config/processor, and all **480 pixel
upscaler LoRA tensor pairs** matching transformer dimensions. These inspect actual
baked model headers and embedded assets; they do not load the full weights on GPU.
A bounded inspection found no supplied HF token or credential variables in the
image configuration/history, and no files at the known credential paths checked.

The original build CLI exited **1 after deliberate cancellation during unpacking**
to avoid filling C:, after all model checks and image export had completed.
The preserved image then unpacked and ran successfully after its temporary build
snapshot was released; post-build verification exited **0**. No models were
redownloaded for this recovery. The build/export/recovery distinction is recorded
in `docs/dfr-verification.json`; a successful original build-process exit is not claimed.

The runtime import/CLI checks passed, **147 worker tests and 73 tester tests passed**,
and 20 DFR tests plus 27 downloader tests passed inside Linux. The real CPU media
fixture preserved nine UHD frames at 24 fps and copied audio packets exactly.
Missing-model startup and the local incompatible-driver preflight rejected the
worker as intended.

To repeat the baked model hash check from PowerShell:

```powershell
$dockerExe = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
& $dockerExe run --rm --pull never --network none --read-only --entrypoint python `
  momensirribrick/worker-ltx25-dfr:4k-v1 /scripts/download_models.py `
  --manifest /models-manifest.json --root /models/ltx25 --verify-only
```

`scripts/dfr_model_smoke.py` is the separate CPU header/tokenizer/LoRA check; mount
it read-only as `/check.py` and run with networking disabled, a writable `/tmp`
tmpfs, and `PYTHONDONTWRITEBYTECODE=1`. The structured verification record includes
the exact checks, timings, source hashes and model inventory.

**DFR GPU generation, full weight loading, S3 delivery from this new image, visual
quality, and peak VRAM still require a Runpod test.** The earlier direct 4K test
had user-reported color artifacts; its successful job status and dimensions did
not establish image quality. Start DFR qualification with 33 frames before 121.
