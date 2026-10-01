# Experimental first/last-frame animation with the CQ model bundle

This workflow adapts the [pinned official Comfy-Org LTX 2.5 first/last-frame template](https://github.com/Comfy-Org/workflow_templates/blob/cce0b679980e4215000f67fe7b12c3a1982310ea/templates/video_ltx2_5_flf2v.json)
to the six weights already installed in the CQ V2 image. It uses the dev INT8
ConvRot transformer, distilled LoRA 450 at 1.0, CQ Enhancer V2 LoRA at 1.0,
convolution video VAE, Gemma INT8 encoder, and audio VAE. CQ's publisher documents
restoration; this combination is an experimental generation adaptation.

The graph is [first_last_frame_cq.json](first_last_frame_cq.json), with input
bindings and validation in [manifest.first-last-cq.json](manifest.first-last-cq.json).
First and last images pass independently through image resize/preprocessing and
`LTXVAddGuide` at frame indices `0` and `-1`. The sampler receives both guides;
`LTXVCropGuides` removes the appended guide latents before decoding. This does
not paste the source images onto the delivered video. Endpoint reconstruction
and rigid architectural geometry are not guaranteed.

Sampling follows the first/last template's eight-step sigma schedule with
`SamplerEulerAncestral` (`eta=0`, `s_noise=1`) and `LTXVDualCFGGuider` (video/audio
CFG 1). Guide strengths default to 0.7, compression to 18, seed to 42, and FPS
to 24. The experiment uses untiled `VAEDecode` with the convolution VAE.

## Integrating into an existing CQ I2V application

Use this graph directly as the starting point. The standard
`workflows/first_last_frame.json` is not the tested CQ graph: it selects a
distilled transformer and standard video VAE, while this graph selects the
installed dev transformer, two LoRAs, and convolution video VAE. Do not infer
the decoder, sampler, or guider settings from the single-image CQ workflow;
the exact tested choices are already present here.

Reuse your existing I2V submission and delivery logic. Add two required image
roles (`first_frame`, `last_frame`), each with a unique filename. Bind every
parameter through the accompanying manifest: dimensions update the video
latent and both image-resize nodes, while frames and FPS also update audio
conditioning. Keep the two guide nodes, their chained conditioning/latent
connections, the dual-CFG guider, and guide cropping before untiled decoding.
The [Python reference compiler](../../tools/runpod_tester/first_last_cq.py)
demonstrates validation, per-role normalization, naming, and output prefixes.
It depends on this repository; port the logic if the application uses another
language.

Submit the resulting `input.workflow` plus `input.images` to the existing
Runpod endpoint. The experimental mode name is an application/compiler label,
not a deployed named worker mode. Apply any exact 720p/1080p crop in the existing
application's delivery pipeline before marking its job complete. Keep job IDs,
asset ownership, credits, secrets, callbacks, and failure handling consistent
with the application's existing I2V integration.

## Local tester

Use a separate tester or retain your key in its masked UI field before restarting:

```powershell
$env:RUNPOD_TESTER_FIRST_LAST_CQ_EXPERIMENT = '1'
$env:MAX_CQ_GENERATION_PIXELS = '3686400'
$env:MAX_CQ_OUTPUT_PIXELS = '3686400'
$env:MAX_CQ_HIGH_RES_FRAMES = '121'
python tools/runpod_tester/server.py --runtime cq-v2 --endpoint dfadob3rm5dg32 --port 8768 --keep-key-in-memory
```

Choose **First and last frames + CQ · experimental**. Each image independently
accepts base64/data URI or HTTPS input through the existing normalizer. The
compiler sends normalized PNGs as `input.images` plus a complete `input.workflow`.
The deployed worker must allow trusted custom workflows. It does not recognize
`first_last_frame_cq_experimental` as a named mode. No new weights or Docker
rebuild are needed for this compatibility path.

```json
{
  "mode": "first_last_frame_cq_experimental",
  "prompt": "A smooth architectural camera move between the two views.",
  "parameters": {
    "width": 2560,
    "height": 1440,
    "num_frames": 121,
    "fps": 24,
    "seed": 42,
    "first_frame_strength": 0.7,
    "last_frame_strength": 0.7,
    "distilled_lora_strength": 1.0,
    "cq_lora_strength": 1.0
  },
  "media": {
    "first_frame": {"base64": "data:image/png;base64,REPLACE_WITH_BASE64"},
    "last_frame": {"url": "https://storage.example.invalid/last.png?REPLACE_WITH_SIGNED_QUERY"}
  }
}
```

This is the input to the **local compiler**, not a request to send directly to
Runpod. Preserve existing job identifiers, S3/R2 output delivery, failure
handling, and credentials kept in server memory/secrets.

## Resolution interpretation

| Requested delivery | Generation canvas | Delivery operation |
|---|---|---|
| 720p | 1280×736 | Crop 8 rows from top and bottom |
| 1080p | 1920×1088 | Crop 4 rows from top and bottom |
| 2560 pixels wide | 2560×1440 | Original generated MP4 |

The graph and tester return the generation canvas. Cropping in the qualification
script is a separate local delivery operation; it is not automatically applied
by this named experiment. All test variants request 121 frames at 24 FPS
(5.041667 seconds). There is no video upscaling, interpolation, or retiming.

The experiment caps width at 2560, height at 1440, and frames at 121; spatial
dimensions must be multiples of 32, and frame counts must be `8n+1`. Larger
canvases need the explicit local pixel/frame overrides above. These limits are
admission settings and do not promise that every permitted input fits a GPU.

## Local verification

All 94 tester tests passed, including six new first/last cases for independent
inputs, guidance wiring, missing-media rejection, parameter limits, opt-in
isolation, and single-attempt legacy transport. The 31-node graph passed schema
and model-reference validation against the pinned ComfyUI/LTX sources. These
checks do not execute generation; live test evidence is recorded separately.

## Live tests, 2026-10-01

All three jobs completed on endpoint `dfadob3rm5dg32`, using the user's two
architectural reference images, the same camera-move prompt, seed 42, CQ 1.0,
distilled LoRA 1.0, guide strengths 0.7, and 121 frames at 24 FPS.

| Delivery resolution | Worker execution | Queue | Result |
|---|---:|---:|---|
| 1280×720 | 58.977 s | 13.517 s | Generated at 1280×736, cropped locally |
| 1920×1080 | 118.380 s | 14.130 s | Generated at 1920×1088, cropped locally |
| 2560×1440 | 280.510 s | 21.426 s | Original Runpod MP4, unchanged |

S3/R2 download, dimensions, all 121 frames, 24 FPS, 5.041667-second duration,
and full decoding passed for every result. The two cropped deliverables use
H.264 CRF16 with audio copied; the QHD deliverable is byte-identical to the
download. No image endpoints were inserted after generation. No new model
download, Docker rebuild, or redeployment was required for these tests.

**Visual limitation:** first and last compositions follow the references closely,
but sampled middle frames show window-grid, facade, and plaza geometry morphing
at all three resolutions. QHD did not eliminate the distortion and introduced
an additional distant background building. Warm colors were broadly consistent
in sampled frames. This verifies execution and delivery, but does not qualify
this image pair for rigid-architecture production work. Exact GPU model and
peak VRAM were not measured; audio was not listened to.

See [sanitized measured evidence](../../docs/first-last-cq-verification.json).
Videos, nine-frame contact sheets, and endpoint comparisons are saved locally
under `artifacts/first-last-cq-20261001/` (ignored by Git).
