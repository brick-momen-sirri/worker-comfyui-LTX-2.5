# Experimental image-to-video with CQ Enhancer V2

For the separate two-image experiment, see [first/last-frame CQ animation](first-last-cq.md).

This is a controlled experiment, not a publisher-supported CQ generation recipe.
[CQ's model card](https://huggingface.co/CQdesign/LTX-2.5-CQ-Video-and-Image-Enhancer-LoRAs)
documents reference-guided restoration and recommends its own workflow. Applying
the video enhancer to ordinary I2V may change colors, composition, motion, or
prompt adherence. Successful execution alone does not establish quality.

For application integration, see the [Momi handoff](../../docs/momi-i2v-cq/README.md)
with complete Runpod request examples, parameter bindings, response handling,
and [sanitized QHD test evidence](../../docs/momi-i2v-cq/verification-2560-121.sanitized.json).

`image_to_video_cq.json` is a ComfyUI API graph using the six weights already in
`models/profiles/cq-v2.json`. No model download or Docker rebuild is required for
an existing CQ image with `ALLOW_CUSTOM_WORKFLOWS=true`. Its model path is:

Dev INT8 ConvRot → distilled LoRA 450 (1.0) → CQ V2 LoRA (0.0 control / 1.0 experiment).

It uses the convolution video VAE, Gemma INT8 encoder, and audio VAE, with ordinary
first-image latent conditioning. The dev-plus-distillation stack is an adaptation
to the existing image inventory; the CQ restoration recipe's distillation
strength is 0.5, whereas this I2V experiment uses 1.0 in both variants. The graph
has no reference-video IC guide and no upscaling stage.

Defaults: 1024×576, 49 frames, 24 FPS, seed 42, eight Euler ancestral steps, CFG 1,
image strength 0.7, compression 18. The local experiment permits up to
2560×1440 and 121 frames, subject to the existing CQ pixel and frame limits. The
default remains 49 frames. For Full HD,
generate at 1920×1088, then crop four rows from both the top and bottom to produce
1920×1080. This crops the generated frames; it does not upscale them. These caps
do not claim broader hardware support.

## Local tester

In PowerShell, start a separate tester or restart the existing one after retaining
your API key in its masked input field:

```powershell
$env:RUNPOD_TESTER_I2V_CQ_EXPERIMENT = '1'
$env:RUNPOD_TESTER_CQ_TRANSPORT = 'workflow'
python tools/runpod_tester/server.py --runtime cq-v2 --endpoint dfadob3rm5dg32 --port 8768 --keep-key-in-memory
```

The default pixel caps admit Full HD. To test 2560×1440 with 121 frames, also set
these explicit local tester overrides before starting it:

```powershell
$env:MAX_CQ_GENERATION_PIXELS = '3686400'
$env:MAX_CQ_OUTPUT_PIXELS = '3686400'
$env:MAX_CQ_HIGH_RES_FRAMES = '121'
```

At 2560×1440 the generated canvas is already 16:9, so no final crop is needed.

Choose **Image to video + CQ · experimental**, upload an image or supply an HTTPS
URL, and enter a motion prompt. Under More settings, use CQ strength 0 for the
control and 1 for the experiment, holding every other setting constant. Each
submission is a separate paid GPU job. This option is hidden unless explicitly
enabled; existing video enhancement behavior is unchanged.

The local server validates inputs and compiles the named experiment to the
existing `input.workflow` plus `input.images` API. **Do not send the experimental
mode name directly to the deployed CQ worker:** its installed named-mode catalog
does not contain it. S3/R2 output delivery and job identifiers use the existing
worker contract.

The local image compiler uses Pillow through the existing media validation code.
No credentials are saved in the workflow or source repository.

## Marina test, 2026-09-30

Two real Runpod jobs completed at 1920×1088, 49 frames, 24 FPS, with the same marina
image, architectural visualization prompt, seed 42, and all settings held equal
except CQ strength. The CQ 1.0 job took 53.930 seconds of worker execution; the CQ
0.0 control took 82.188 seconds. These timings include different worker/cache
conditions and do not establish a CQ speed advantage.

Both R2 output downloads, frame counts, dimensions, FPS, durations, and full video
decodes were verified. Final videos were cropped to 1920×1080. Sampled frames show
crisper water and boat detail with CQ, together with a changed camera trajectory.
This verifies execution for this particular experiment, not general I2V quality
or preservation of exact architectural details. GPU model and peak VRAM were not
measured. All 87 local tester tests passed.

Local results and the verification report are under
`artifacts/marina-i2v-cq-1920/` (ignored by Git). The saved API graphs reproduce the
model settings; replace their job-specific image filename/output prefix when
reusing them. No Docker image was rebuilt or redeployed for this test.

The follow-up 121-frame CQ 1.0 test also completed on 2026-09-30 with the same
image, prompt, seed, and sampling settings. Worker execution took 110.357 seconds
after 20.751 seconds in the queue. Both the raw 1920×1088 video and the cropped
1920×1080 deliverable were verified to contain 121 frames at 24 FPS (5.041667
seconds), with successful R2 download and full decode checks. Nine sampled
frames show coherent broad building forms and consistent overall color; this
does not establish exact preservation of every architectural detail or moored
boat position. The five targeted I2V transport tests passed after raising the
experimental cap to 121; the default stays at 49. Results and evidence are in
`artifacts/marina-i2v-cq-1920-121/`.

The 2560×1440 follow-up also completed in one 121-frame job on 2026-09-30, using
CQ 1.0 at 24 FPS with the same image, prompt, seed, and sampling settings. Only
the latent canvas and conditioning-image dimensions changed. Worker execution
took 219.420 seconds after 14.344 seconds in the queue. The R2 download contains
all 121 frames (5.041667 seconds); dimensions, FPS, and full decode were verified.
The delivered MP4 is byte-identical to that download, with no output resizing,
cropping, retiming, interpolation, or re-encoding. Nine sampled frames show
consistent overall colors and coherent broad building forms, with moving boats
and water. Exact preservation of scene details is not established. GPU model
and peak VRAM remain unmeasured. All six targeted I2V transport tests passed.
The original result and verification report are under
`artifacts/marina-i2v-cq-2560-121/`.

## Publication checks

Before publishing the experiment, all **88 local tester tests** passed, including
the six I2V cases. The 26-node experimental graph passed validation against the
pinned ComfyUI/LTX source and CQ model inventory. The Momi request examples were
checked against the source graph and manifest, and their JSON/checksums passed.
JavaScript syntax, all 21 standard workflow structures, and Git whitespace checks
also passed. These publication checks ran locally and submitted no new GPU jobs.
