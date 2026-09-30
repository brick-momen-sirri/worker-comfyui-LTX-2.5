# CQ Enhancer V2 ComfyUI image

`cq-v2` is a separate ComfyUI build profile for `ltx2.5-CQ-enhancer-lora-V2.safetensors`. It does not alter the existing INT8 ComfyUI image or the standalone DFR 4K image. Build it with a separate tag and deploy it to a separate Runpod Serverless endpoint.

## Verified publisher recipe

The implementation is pinned to CQdesign repository revision `6495196bc21b2e24615b27e480b30049ca4efeea` and the publisher's **LTX2.5 - CQ Enhancer lora video workflow V2**. The publisher describes this as generative video restoration rather than conventional pixel upscaling, says a prompt is unnecessary, requires the convolution VAE, and supplies a new workflow for V2. The pinned source graph uses:

- LTX 2.5 **dev** transformer, Comfy INT8 ConvRot;
- LTX 2.5 distilled LoRA 450 at `0.5`;
- CQ Enhancer V2 LoRA at `1.0`;
- convolution video VAE and the LTX audio VAE;
- empty positive and negative prompts, CFG `1.0`, Euler ancestral, and the publisher's fixed eight-step sigma schedule;
- source video forced to `30 FPS`, at most `153` frames, shorter side resized to about 720 pixels, and dimensions aligned to 32;
- source video frames as an IC-LoRA guide at strength `1.0`; and
- original source audio in the rendered result.

The API graph is [video_enhance_cq_v2.json](../workflows/video_enhance_cq_v2.json), and its named-mode contract is [manifest.cq-v2.json](../workflows/manifest.cq-v2.json). The graph replaces UI-only Video Helper Suite, KJ loader, Fill switch, and ComfyMath plumbing with pinned ComfyUI core nodes and worker-side FFmpeg normalization. The publisher's `LTXVImgToVideoConditionOnly` has `bypass=true`, so the translated graph connects the empty latent directly to the IC-LoRA guide. The KJ SageAttention patch is omitted; pinned ComfyUI uses its native optimized attention path. These substitutions passed node and input schema validation against the pinned ComfyUI and LTX custom-node sources. Exact visual equivalence and memory use still require the Runpod GPU test.

The publisher also supplies a 25 FPS output workflow that uses an additional frame-rate conversion custom node. This image implements the primary V2 30 FPS recipe. It does not claim the separate 25 FPS conversion workflow or the publisher's separate image-enhancer LoRA.

## Model inventory

All model files are downloaded while the image is built and checked by exact length and SHA256. Runtime downloads are disabled.

| Model | Bytes | SHA256 |
| --- | ---: | --- |
| `diffusion_models/ltx-2.5-22b-dev-transformer-comfy-int8-convrot.safetensors` | 21,504,034,224 | `2edbdb4465cd6c3b532cd67a31ddb38a63e97dcad20be3729675e2a4e8caf92b` |
| `text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors` | 15,372,969,374 | `6ce688a0aa98a5fa36a9f1e6c3f42152a498cc2b53ee8c15674c64244f91487f` |
| `loras/ltx-2.5-22b-distilled-lora-450-bf16.safetensors` | 8,899,889,568 | `86370bbf79a9eb4edaa158907e2b48a5188fe4c5dc8ce30c7eb8f2f131a9bbf5` |
| `loras/ltx2.5-CQ-enhancer-lora-V2.safetensors` | 1,340,583,888 | `bc0924477007509db63a5a1a6c51c83194e724e8db6f04bc1fc3ba8225a5b730` |
| `vae/ltx-2.5-video-vae-conv-bf16.safetensors` | 1,452,269,922 | `685b06ee3d9b2039647698fc4ea33175112462fc374e2777312c907897dfce8d` |
| `vae/ltx-2.5-audio-vae-bf16.safetensors` | 364,866,540 | `c52733d37f6a7fb7949c3dc0fb468c6cb2169e4d836983a73babb9f0d54837a5` |

Total: **48,934,613,516 bytes (48.93 GB / 45.57 GiB)** in six weight files.

The five LTX files use Lightricks revision `5e6e71018ee1756ed329b697a7b4aedc934dfce9`. The CQ LoRA and source workflow use CQdesign revision `6495196bc21b2e24615b27e480b30049ca4efeea`.

## API

Submit this named input to the Runpod `/run` endpoint. `video` independently accepts raw base64, a `data:video/...;base64,...` URI, or an HTTPS URL such as a presigned S3 URL.

```json
{
  "input": {
    "mode": "video_enhance_cq_v2",
    "media": {
      "video": {
        "url": "https://bucket.example/source.mp4?X-Amz-Signature=REDACTED"
      }
    },
    "parameters": {
      "width": 1280,
      "height": 704,
      "num_frames": 33,
      "fps": 30,
      "seed": 42,
      "cfg": 1.0,
      "distilled_lora_strength": 0.5,
      "cq_lora_strength": 1.0,
      "guide_strength": 1.0
    }
  },
  "policy": {
    "executionTimeout": 3600000,
    "ttl": 7200000
  }
}
```

`prompt` and `negative_prompt` may be omitted. The publisher recipe leaves both empty. Supported frame counts are `8n+1` from 9 through 153. FPS is fixed at 30. Width and height must be multiples of 32 from 256 through 2560; the default CQ pixel cap remains 2,088,960 (`MAX_CQ_GENERATION_PIXELS`). Canvases above that Full HD area require explicit pixel-cap opt-in and default to a 25-frame limit (`MAX_CQ_HIGH_RES_FRAMES`); raising that configurable admission limit requires GPU qualification. Existing `cq-v2` images retain their original 1536-side limit for named requests until rebuilt. The defaults remain 1280×704, 153 frames, seed 42, and the strengths shown above. Start qualification with 33 frames at the default canvas, then increase duration only after GPU verification.

For Full HD inference, request **1920×1088**, then crop four rows from the top and bottom of the generated frames to deliver 1920×1080. The denoiser and video VAE operate at 1920×1088; this is not a resize of a smaller generated result. It is a larger experimental canvas than the publisher's approximately 720-pixel short-side preset, not a publisher-verified Full HD recommendation. Start with 33 frames and use short overlapping segments for longer clips. The frame and pixel admission limits do not guarantee a particular workload fits in VRAM.

For experimental **2560×1440** inference, set both `MAX_CQ_GENERATION_PIXELS=3686400` and `MAX_CQ_OUTPUT_PIXELS=3686400` in the process validating the named request. With local workflow transport, these belong on the local tester; with a rebuilt named-mode image, set them on the worker. The separate CQ output cap falls back to `MAX_OUTPUT_PIXELS` if unset, preserving existing limits. The high-resolution frame limit defaults to **25**; set `MAX_CQ_HIGH_RES_FRAMES=121` to admit a one-job 121-frame experiment. The manifest's 153-frame maximum still applies. Neither dimension needs an output crop. This uses the same six weights and eight-step CQ graph. It is an experimental extension of the publisher's canvas, not a publisher guarantee of 1440p quality or 48 GB memory fit. See `examples/video_enhance_cq_v2_2560.json`.

To preserve a 121-frame / 24 FPS source while using the CQ recipe's 30 FPS conditioning, first retime all source frames to 30 FPS without inserting or dropping frames, run one 121-frame CQ job, then retime all generated frames to 24 FPS. This preserves the 5.041667-second presentation duration. Simply applying a 30 FPS conversion to the original and taking 121 frames would omit the source's final second. The retiming method changes internal motion timing relative to the publisher's normal 30 FPS input recipe and must be evaluated for quality; it is not native 24 FPS model conditioning.

The local tester can submit its bundled CQ graph through an existing image's original workflow API when that endpoint already permits legacy workflows. This uses the same six installed weights and output-upload contract, and needs no image rebuild. Start the tester with `RUNPOD_TESTER_CQ_TRANSPORT=workflow` in its environment. It validates the named request and normalizes video locally with FFmpeg before sending the trusted graph; Pillow and FFmpeg/ffprobe must be installed locally. It does not accept caller-supplied graphs or enable custom workflows on the endpoint. If `ALLOW_CUSTOM_WORKFLOWS=false`, the request is rejected: rebuild and deploy the updated image, then use the default `RUNPOD_TESTER_CQ_TRANSPORT=named` instead. Do not weaken the endpoint's production API policy merely for this compatibility path.

The worker decodes the source, resamples it to 30 FPS, resizes and center-crops it to the selected dimensions, trims it to the exact requested frame count, and normalizes audio. A source without audio receives silence so the existing output path stays valid. The input must be at least `num_frames / 30` seconds long. For a 16:9 source, 1280×704 follows the publisher's approximately 720-pixel short-side preprocessing after 32-pixel alignment. Choose a different aligned canvas for portrait or other aspect ratios.

Successful responses retain the existing worker contract and the original Runpod job ID:

```json
{
  "status": "COMPLETED",
  "output": {
    "success": true,
    "job_id": "RUNPOD_JOB_ID",
    "prompt_id": "COMFYUI_PROMPT_ID",
    "videos": [
      {"filename": "LTX25_00001_.mp4", "type": "s3_url", "data": "https://..."}
    ],
    "credit_usage": {}
  }
}
```

Validation and delivery failures use the existing error contract. For example, a video shorter than the requested duration fails before generation:

```json
{
  "status": "FAILED",
  "error": "Video is too short for num_frames at the requested fps",
  "error_code": "INVALID_MEDIA"
}
```

Completion is returned only after ComfyUI generation and every required S3 output upload succeeds. Temporary inputs and outputs are removed after successful delivery; failure retains evidence and requests worker recycling, matching the existing integration.

## Build and deployment

The build needs no GPU. Use the WSL Docker engine, which has enough storage, and keep the required `linux/amd64` platform:

From PowerShell, save the token without displaying it or putting it in shell history:

```powershell
& .\scripts\save-hf-token.ps1
```

Then run the build from WSL using that ignored secret file:

```bash
cd /mnt/d/worker-comfyui-LTX-2.5
docker buildx build --platform linux/amd64 \
  --build-arg MODEL_PROFILE=cq-v2 \
  --secret id=hf_token,src=/mnt/d/worker-comfyui-LTX-2.5/.secrets/hf_token \
  --tag momensirribrick/worker-comfyui-ltx25:cq-v2 \
  --load .
docker image inspect momensirribrick/worker-comfyui-ltx25:cq-v2 \
  --format '{{.Id}} {{.Size}} {{index .Config.Labels "io.ltx.model-profile"}}'
```

The separate **private** repository `momensirribrick/worker-comfyui-ltx25-cq` now contains the verified `cq-v2` image. It was pushed successfully on 2026-09-29. Authenticated registry inspection confirms Linux/AMD64 and index digest `sha256:9bd09e9b9ee2386feb0e09d138be8a761e5faf61512897ea9140c667617b3c8a`, matching the local build. Anonymous access returned HTTP 404 before and after the push. No rebuild is needed to deploy this image.

For a future push of the same local build, use:

```bash
docker login
docker tag momensirribrick/worker-comfyui-ltx25:cq-v2 \
  momensirribrick/worker-comfyui-ltx25-cq:cq-v2
docker push momensirribrick/worker-comfyui-ltx25-cq:cq-v2
```

To deploy the exact verified version independently of future tag changes, use this image reference:

```text
momensirribrick/worker-comfyui-ltx25-cq@sha256:9bd09e9b9ee2386feb0e09d138be8a761e5faf61512897ea9140c667617b3c8a
```

The CQ repository is public and ungated, but its Hugging Face metadata declares no license. LTX itself uses the LTX-2.x Community License. Keep the registry repository private unless you have permission to redistribute the CQ LoRA inside a public Docker image.

Plan for **150–200 GB free Docker build storage**. The six weights alone are 45.57 GiB; package, source, layer export, and temporary snapshots add substantial overhead. For Runpod, start with an **RTX 6000 Ada 48 GB**, **64 GB or more system RAM**, **120 GB or more container disk**, **30–50 GB writable scratch**, and **16 GB shared memory**. The profile's default startup admission floor is 47 GiB VRAM. A 24 GB local GPU is below that floor and is not the target for the publisher's 720p/153-frame recipe.

For the completed local build, Docker measured 45.89 GB through `image inspect`, 56.57 GB as the container's apparent root filesystem, and 63.88 GB of local image storage. Docker's list view showed a 102 GB virtual size. These are different accounting views of the same image; keep the larger build-space recommendation because export, cache, and unpacking temporarily coexist.

Create a separate queue-based Serverless endpoint with one active job per worker. Use `momensirribrick/worker-comfyui-ltx25-cq:cq-v2` as the container image. In Runpod's Container Registry settings, add Docker Hub credentials with username `momensirribrick` and a Docker Hub access token with read access, then attach that registry credential to the endpoint's template. Private-image pull credentials belong in the registry configuration, not in worker environment variables. See [Runpod's private-image deployment guidance](https://github.com/runpod/runpod-plugins-official/blob/main/plugins/runpod/skills/runpod/golden-paths/05-model-to-endpoint-pipeline.md).

Select a host with a CUDA 13 compatible NVIDIA driver (R580 or newer), matching the built PyTorch CUDA 13 runtime. Configure the same S3 output secrets used by the existing worker: `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY`, and optional `BUCKET_REGION`. Set `ALLOW_CUSTOM_WORKFLOWS=false`, `WORKFLOW_EXECUTION_TIMEOUT_S=3600`, `COMFY_RESERVE_VRAM=2`, and give the Runpod request a longer timeout than input download, generation, and output upload.

Launch the local test page against the CQ endpoint:

```bash
cd /mnt/d/worker-comfyui-LTX-2.5
python3 tools/runpod_tester/server.py --runtime cq-v2 \
  --endpoint YOUR_CQ_ENDPOINT_ID --port 8767
```

The tester exposes only `video_enhance_cq_v2`, begins at 33 frames, accepts local/base64 or S3 URL video, and assigns a one-hour execution timeout and two-hour TTL.

## Verification status

- **Single-job 1440p / 121 frames passed on September 30:** job `f57a39b5-1920-40df-b8e8-2b47d22c16e4-e1` on endpoint `dfadob3rm5dg32` completed in **687.902 seconds** after a **20.463-second** queue. One CQ pass produced **2560×1440 / 121 frames / 30 FPS**, delivered through R2. All 121 source frames were retimed to 30 FPS before inference; all 121 output frames were retimed to **24 FPS / 5.041667 seconds** afterward. No generation sections, output resizing, output cropping or frame interpolation were used. Raw, final and comparison videos passed complete decoding and exact dimensions/frame counts. Nine output frames and first/middle/last comparisons were inspected: sharper detail, retained broad geometry, and some color/texture changes; no claim of perfect temporal fidelity. This required only `MAX_CQ_HIGH_RES_FRAMES=121` on the local compatibility tester, alongside the existing 1440p pixel caps. The default admission limit remains 25. No new models, Docker build, or remote setting change. Peak VRAM and the GPU model were not measured, and 153-frame capacity remains untested. The cap change passed **53 targeted unit tests**, Python compilation and JavaScript syntax. See [single-job evidence](cq-v2-2560-single121-verification.json) and `examples/video_enhance_cq_v2_2560_121.json`.

- **1440p AI inference passed on September 29:** seven **25-frame / 2560×1440 / 30 FPS** CQ jobs on endpoint `dfadob3rm5dg32` completed and delivered videos through R2. Each took 89–97 seconds of reported execution time. The source `SHOT_1130_v001` was processed in overlapping sections; the final file preserves **121 frames / 24 FPS / 5.041667 seconds** at 2560×1440. All raw outputs, the final file, and the comparison passed full decoding and exact dimension/frame checks. No generated output was spatially resized or cropped. Sampled frames show sharper cladding and foliage, with changes in color, contrast and texture. Section joins have larger adjacent-frame changes; seamless motion is not established. This qualifies 25-frame sections, not a single full-length 1440p job, measured peak VRAM, or a specific verified GPU model. Same six weights, no Docker rebuild or remote configuration change. Local changes passed **64 targeted unit tests**, JavaScript syntax and example validation. See [1440p evidence](cq-v2-2560-verification.json).

- **Full HD AI inference passed on September 29:** one 33-frame and four 49-frame jobs on endpoint `dfadob3rm5dg32` returned newly generated **1920×1088 / 30 FPS** videos through R2. A 960×540 source was processed as five overlapping sections. After removing duplicate overlap frames and cropping four rows at each edge, the final file is **1920×1080 / 200 frames / 6.666667 seconds**. No generated output was spatially resized. Each raw result and the final assembled video passed complete decoding and frame-count checks. First/middle/last frames and both sides of every join were inspected; details change and some motion blur remains. This is limited qualification of 33/49-frame sections, not proof of 153-frame Full HD capacity or exact fidelity. No model rebuild or endpoint configuration change was required: the deployed endpoint already accepted its original workflow API. See [Full HD evidence](cq-v2-1920-verification.json). The local admission/transport changes passed **62 targeted unit tests**, the new request example parsed, Python compiled, and JavaScript syntax passed.

- **First Runpod CQ generation passed on September 29:** job `8520dc98-2010-4fdf-864b-502ac2a1f1f3-e2` on endpoint `dfadob3rm5dg32` completed in **50.481 seconds**, after **798.243 seconds** queued during the GPU configuration correction. A 320×176 Big Buck Bunny sample sent as base64 produced **1280×704 / 33 frames / 30 FPS / 1.1 seconds**, with H.264 video and matching-duration stereo AAC audio. R2 output download, full FFmpeg decode, exact frame count, and browser loading all passed. The output is 1,880,096 bytes. The first, middle and last frames were visually inspected; details are sharper, but lighting, colors and fine structures also change. This is a generative enhancement, not an exact recovery of the source.
- The initial workers exposed **31.4 GiB total VRAM**, below the configured **47 GiB** admission floor, and exited before accepting jobs. The operator changed the GPU settings and the same queued job then completed on worker `4ihr6o8sbu3t1f`. Use only GPU categories with at least 48 GB per assigned device, excluding smaller fallback categories. No image rebuild was needed. The floor is an operator policy, not a measured universal model minimum. The successful job response did not report its GPU model, total VRAM or peak usage, so those hardware measurements are not independently confirmed. [Runpod GPU configuration](https://docs.runpod.io/serverless/endpoints/endpoint-configurations#gpu-configuration).
- Exact model metadata, pinned revisions, sizes, and SHA256 records: verified.
- Publisher V2 graph and current model card requirements: inspected.
- Translated API graph structure and all node/input names: passed against the pinned ComfyUI and LTX source schemas. [Validation report](cq-v2-workflow-validation.json).
- Worker and tester unit suites: **150 + 75 passed** locally after the final CQ changes; Python compilation and tester JavaScript syntax also passed.
- Model-free `linux/amd64` Docker runtime: built with exit code 0; ComfyUI 0.35.0 registered 1,003 nodes and live-schema validation passed the CQ graph. No weights were loaded.
- Full six-model `linux/amd64` image: built and loaded successfully as `momensirribrick/worker-comfyui-ltx25:cq-v2`; all **48,934,613,516 model bytes** passed pinned download hashes and final in-image size verification.
- Private Docker Hub upload: completed with exit code 0 as `momensirribrick/worker-comfyui-ltx25-cq:cq-v2`. Authenticated remote inspection confirmed the local build's exact index digest and Linux/AMD64 platform; anonymous repository access remained unavailable.
- Final image security/startup: profile label and model catalog passed, the Hugging Face token is absent from image metadata/history and token paths, and the complete image became healthy on the local RTX 4090 with a startup-only admission override. Its normal 47 GiB floor correctly rejects that 24 GiB card.
- Real worker API smoke: `/runsync` assigned a job ID and rejected an intentionally missing video before model loading. No LTX weights were loaded and no generation ran.
- Full 153-frame generation, peak VRAM, exact equivalence to the publisher's original GUI workflow, and cloud failure recovery remain untested. The successful 33-frame sample establishes short CQ inference and R2 delivery, not those broader claims.

The complete status is recorded in [test-results.md](test-results.md) and [cq-v2-verification.json](cq-v2-verification.json).
