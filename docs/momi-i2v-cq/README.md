# Momi integration: LTX 2.5 image-to-video with CQ Enhancer V2

Prepared 2026-09-30. Scope: integrate the tested marina I2V workflow into Momi.

## Instructions for the next AI

Inspect Momi'snow pleas commit and push the all naccrly stof to the githup repo  existing frontend, backend, authentication, asset storage, job
tracking, billing/credits, and callbacks first. Integrate this workflow into
those mechanisms. This package describes the working Runpod integration; it
does not contain a completed Momi integration or assume a Momi framework.
Keep provider credentials on the backend. Implement validation, submission,
status handling, result ingestion, and user-facing errors. Preserve existing
Momi behavior and test against its actual interfaces.

## 1. WHAT THIS WORKFLOW DOES

An input image plus a motion/scene prompt generates an MP4 with video and audio.
The model chain is LTX 2.5 Dev INT8 ConvRot -> distilled LoRA 450 at 1.0 -> CQ
Enhancer V2 LoRA at 1.0. CQ strength 0 gives a control without the CQ effect.
It uses ordinary first-image conditioning, one sampling stage, and no video
upscaling stage. The tested 2560x1440 output was generated at that canvas size.
ImageScale resizes the CONDITIONING IMAGE; it does not upscale an output video.

This is an experimentally tested adaptation of CQ, whose publisher documents
reference-guided restoration. It is not a publisher-certified ordinary I2V
recipe. Do not confuse it with the existing video_enhance_cq_v2 restoration
workflow, which uses different conditioning, FPS, and distilled-LoRA strength,
or with the official staged 4K workflow. Do not promise exact architectural
geometry preservation. The successful test does not establish all-input quality.

## 2. DEPLOYMENT AND THE CRITICAL API DIFFERENCE

Repository: https://github.com/brick-momen-sirri/worker-comfyui-LTX-2.5
Runpod endpoint ID: dfadob3rm5dg32
Base URL: https://api.runpod.ai/v2/dfadob3rm5dg32
Existing private image: momensirribrick/worker-comfyui-ltx25-cq:cq-v2
Last verified digest:
sha256:9bd09e9b9ee2386feb0e09d138be8a761e5faf61512897ea9140c667617b3c8a

The deployed image already contains the six required weights and accepts the
legacy input.workflow + input.images interface. No Docker rebuild is needed
to use that interface with this graph. ALLOW_CUSTOM_WORKFLOWS must be true.

DO NOT send the named mode image_to_video_cq_experimental directly to Runpod.
Only the local tester currently knows that mode; it compiles it into a graph.
Momi's backend must perform that compilation and submit the complete graph.
Adding the named mode to the deployed worker would be separate implementation
and require updating the selected CQ manifest and rebuilding/redeploying.

The experimental implementation and this handoff are included in this
repository. The mode remains opt-in in the local tester and is not added to
the deployed worker's named-mode catalog. This directory carries the tested
graph and contract so an AI working elsewhere can reproduce the request.
Repository source files worth consulting:
  tools/runpod_tester/i2v_cq.py
  tools/runpod_tester/test_i2v_cq.py
  workflows/experimental/image_to_video_cq.json
  workflows/experimental/manifest.i2v-cq.json
  ltx_worker/adapter.py and ltx_worker/media.py
  handler.py
The local compiler depends on this repository; it is not a standalone module.
The localhost tester at port 8768 is a development tool, not a production API.

## 3. PACKAGE CONTENTS

All files listed below are in this directory. [Workflow graph](workflow-api.json),
[base64 request](runpod-request-base64.example.json),
[URL request](runpod-request-url.example.json),
and [verified QHD test](verification-2560-121.sanitized.json).

workflow-api.json: complete ComfyUI API-format graph at 2560x1440 / 121 frames.
input-contract.json: original experimental defaults, constraints, and bindings.
  Its 'file' points into the source repo. It is a reference manifest, not a
  standalone loader configured for this package. The manifest defaults remain
  1024x576 / 49 frames; the packaged tested graph intentionally uses 2560/121.
runpod-request-base64.example.json: full Runpod request, replace media placeholder.
runpod-request-url.example.json: alternative validated PNG via signed HTTPS URL.
momi-input.example.json: conceptual input to Momi's compiler, NOT Runpod's API.
runpod-success.sanitized.json: reduced actual successful response without secrets.
runpod-failure.illustrative.json: hypothetical failure shape, not a failed test.
verification-2560-121.sanitized.json: actual test evidence, local paths removed.
models.cq-v2.json: exact six model files, repository revisions, sizes, hashes.
sources.lock.json: pinned ComfyUI and custom-node versions.
archviz-prompt.txt: exact tested prompt.
No model weights, source image, output video, credentials, or live signed URLs
are included. Placeholder examples require replacement before submission.

## 4. PARAMETERS AND PRESETS

Tested QHD: width=2560, height=1440, num_frames=121, fps=24.
Tested Full HD: generate 1920x1088, then crop 4 rows from top and bottom to
1920x1080. This postprocessing belongs in Momi's backend if offering exact 1080p.
121 frames at 24 FPS lasts 5.041667 seconds; 49 frames lasts 2.041667 seconds.

Exact sampling: seed 42, CFG 1, 8 Euler ancestral steps, image strength 0.7,
image compression 18, distilled LoRA 1.0, CQ LoRA 1.0, batch size 1.
Manual sigmas are fixed in the graph. Do not replace them with an arbitrary
steps field or change model/sampler settings in the initial integration.

Validate width 256..2560 and height 256..1440, both multiples of 32. Enforce
total pixels <= 3,686,400 and frames 9..121 satisfying (frames-1) % 8 == 0.
FPS is 24 only, CFG is 1 only. Image/LoRA strengths are finite numbers 0..1;
compression is an integer 0..100. Require a nonempty prompt, at most 12,000
characters; negative prompt has the same length cap. Reject unknown settings.
Use nonnegative safe integer seeds in JavaScript (at most 2^53-1), unless the
backend deliberately preserves larger uint64 values without precision loss.

The original compiler's 2560/121 opt-in overrides are:
  MAX_CQ_GENERATION_PIXELS=3686400
  MAX_CQ_OUTPUT_PIXELS=3686400
  MAX_CQ_HIGH_RES_FRAMES=121
These admission checks run in the compiler. Do not assume the remote legacy
workflow API applies them. Enforce equivalent limits in Momi's backend, even
when bypassing the local Python compiler. Other configured global caps may
lower them. Prefer the two tested landscape presets initially.

## 5. GRAPH COMPILATION

Clone workflow-api.json for each job. input-contract.json lists all bindings.
Update width/height in BOTH video_latent and image_resize; update frames in
BOTH video_latent.length and audio_latent.frames_number. FPS binds to
conditioning.frame_rate, audio_latent.frame_rate, and video.fps.
Prompt -> positive.text; negative -> negative.text; seed -> sample_noise.noise_seed.
Image strength -> image_condition.strength; compression -> image_preprocess.img_compression.
LoRA strengths -> distilled_lora.strength_model and cq_lora.strength_model.
Keep sample_guider.model wired to cq_lora. Use a fresh safe filename such as
ltxi2vcq-<uuid>.png in BOTH load_image.image and input.images[0].name. Generate a
unique save.filename_prefix per Momi job. Never share mutable graph objects
across concurrent submissions or accept arbitrary graphs from browser clients.

## 6. MEDIA AND JOB SUBMISSION

Validate input asset ownership. Decode and normalize images to PNG server-side.
The existing normalizer accepts raw base64, data URIs, or HTTPS URLs, with default
limits of 20 MiB and 40 million decoded pixels. Reject invalid or oversized
images and failed downloads before launching a paid GPU task. Use equivalent
bounded download/SSRF protections if porting it. A file extension is not validation.

Live I2V tests used normalized PNG encoded as raw base64 under:
  input.images = [{"name":"unique.png","image":"<raw base64 PNG>"}]
The worker also implements URL transport:
  input.images = [{"name":"unique.png","url":"<signed HTTPS PNG URL>"}]
That URL variant is supported in code, but was not separately GPU-tested in
this I2V experiment. Use trusted Momi storage and normalize before signing.
Allow URL expiry to cover queueing plus execution. Base64 adds about 33% size;
use signed storage URLs if the encoded request exceeds the API/application
limit. The local tester has a 9,000,000-byte request cap, not a claim about
every Runpod deployment's limit.

POST /run on the base URL with Authorization: Bearer <RUNPOD_API_KEY> and
Content-Type: application/json. The included examples contain the full graph.
Use policy.executionTimeout=3600000 and policy.ttl=7200000 (milliseconds), as
in the tested path. Ensure the worker's own execution timeout is sufficient;
WORKFLOW_EXECUTION_TIMEOUT_S is in seconds, not milliseconds.

Save the returned Runpod id immediately against the original Momi task/user.
Use an application idempotency key and submission record. Do not blindly retry
POST /run after a connection timeout: the first request may already be accepted.
Reconcile ambiguous submissions rather than creating duplicate paid jobs.

## 7. STATUS, DELIVERY, STORAGE, AND FAILURES

Poll GET /status/{runpod_job_id} from Momi's backend, initially every 5-10 seconds
with backoff. A top-level Runpod webhook is an alternative if Momi already
supports it, but webhooks were not used/tested in these experiments. Protect
any webhook and verify its job association before changing application state.

Runpod COMPLETED alone does not establish generation success. Require
output.success === true, no output.error or relevant errors, and a nonempty
output.videos array. The tested videos item contains filename, media_type
'video', type 's3_url', and data holding the presigned result URL. Treat empty
outputs and success_no_outputs as unsuccessful for this video feature. Handle
FAILED, CANCELLED, TIMED_OUT, and COMPLETED-with-worker-error distinctly.

The existing handler uploads outputs before returning success and keeps the
Runpod job ID in its storage prefix. Preserve prompt_id, credit_usage,
comfy_credits, and other response fields when present; do not invent replacement
credits or confuse the Comfy prompt ID with the Runpod ID. The sanitized example
omits additional metadata fields for clarity.

The deployed worker already uploads to S3-compatible storage/R2 using:
  BUCKET_ENDPOINT_URL
  BUCKET_ACCESS_KEY_ID
  BUCKET_SECRET_ACCESS_KEY
Preserve that deployment's endpoint/bucket layout and credentials. Do not add
an assumed BUCKET_NAME or per-request s3Config and expect this handler to use it.
Keys belong in Runpod secrets or Momi backend secrets, never browser code,
workflow JSON, source control, or logs. Signed URLs also need log redaction.

Store the completed asset in Momi's durable asset system, or retain its durable
object key and re-sign URLs. Worker URLs are temporary (the existing uploader
uses a seven-day expiry, potentially shortened by credentials). Fetch/persist
the Runpod result promptly: async /run response data is retained for only
30 minutes after completion, independently of object URL expiry. Only mark the
Momi task completed after required result ingestion/cropping/upload finishes.
Clean temporary inputs after delivery and preserve outputs on delivery failures.

## 8. VERIFIED TESTS AND LIMITATIONS

Marina CQ I2V jobs on 2026-09-30:
  1920x1088, 49 frames, CQ=1: execution 53.930 seconds.
  1920x1088, 49 frames, CQ=0: execution 82.188 seconds (control).
  1920x1088, 121 frames, CQ=1: execution 110.357 seconds.
  2560x1440, 121 frames, CQ=1: execution 219.420 seconds; queue 14.344 seconds.
These are observations, not latency guarantees or evidence that CQ makes it
faster. Worker/cache conditions differed. All used 24 FPS and seed 42.

Latest job: b4edcbd6-ece5-46f2-9039-a5342eefd447-e2.
Verified: R2 download; 2560x1440; all 121 frames; 24 FPS; duration 5.041667s;
audio track present; full video decode; single generation job. The delivered
MP4 is unchanged from Runpod, with no output resizing, cropping, retiming,
interpolation, or re-encoding. SHA-256:
ad15ed54c12ddf76adda89cf9a07d40e08365f6053a8f4c40248e51876c3f50a

Six targeted compiler/transport tests passed after QHD support. Earlier local
tester suite: 87 tests passed. No Docker rebuild was needed for these I2V runs.
Nine sampled QHD frames showed consistent broad forms/colors, with moving
boats/water. Exact geometry is not guaranteed; audio was not listened to.
User reports a 48 GB worker. Exact GPU model and peak VRAM were not measured.
This I2V mode has no validated 4K, >121-frame, 16 GB, or 24 GB support from these
tests. The 1024x576 default was validated locally, not GPU-tested in this series.

## 9. MODEL INVENTORY AND REPRODUCIBILITY

The six files are the dev INT8 transformer, Gemma INT8 text encoder, distilled
LoRA 450, CQ Enhancer V2 LoRA, convolution video VAE, and audio VAE. Total model
bytes: 48,934,613,516 (~45.57 GiB); this is disk weight size, not VRAM use or full
Docker size. Use exact paths/revisions/hashes in models.cq-v2.json. The
convolution VAE is part of the tested recipe; do not swap to the standard VAE.
ComfyUI v0.35.0 and custom nodes are pinned in sources.lock.json. If rebuilding
for Runpod later, always use --platform linux/amd64. No build is needed just
to have Momi submit this existing graph to the currently deployed image.

## 10. ACCEPTANCE CHECKLIST FOR THE MOMI IMPLEMENTATION

Validate all parameters and ownership before submission. Test missing/invalid
images, bad URLs, unsupported dimensions/FPS/frames, concurrent graph isolation,
secret redaction, uncertain submissions, provider failures, output upload or
ingestion failures, expired URLs, and COMPLETED without a usable video. Verify
the job belongs to its caller, metadata/credits are preserved, and the original
caller sees its completed asset only after delivery succeeds. Run one explicit
live smoke test once implemented and record job ID, dimensions, frame count,
FPS, playback/decode, and delivery. Do not claim that a Momi integration already
exists or that these package-only checks tested Momi.

## References

Project: https://github.com/brick-momen-sirri/worker-comfyui-LTX-2.5
Runpod requests/policies/results: https://docs.runpod.io/serverless/endpoints/send-requests
Runpod states: https://docs.runpod.io/serverless/endpoints/job-states
CQ publisher: https://huggingface.co/CQdesign/LTX-2.5-CQ-Video-and-Image-Enhancer-LoRAs
LTX weights: https://huggingface.co/Lightricks/LTX-2.5
Implementation claims come from the local code and saved test reports. Provider
API facts should be rechecked if integrating against a later version.
