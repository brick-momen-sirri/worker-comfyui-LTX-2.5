# Direct 4K image-to-video experiment

**Quality finding, September 12:** the user reported color artifacts in the 121-frame result. The cause is undiagnosed. Execution success below establishes timing, dimensions, and delivery only. Use the separate [official DFR implementation](dfr-4k.md) for the next quality test.

`image_to_video_native_4k` completed real **nine-frame and 121-frame direct-4K generations** on the user's 48 GB Runpod worker, with successful S3 delivery and browser media loading. Execution took **73.208 seconds** and **17 minutes 21.038 seconds**, respectively. It uses **one diffusion stage at 3840×2176**, followed by a center crop to **3840×2160**, with the same bundled official INT8 transformer, text encoder, and existing VAEs. **No LoRA, latent upscaler, or pixel-upscaling stage is used.** No additional models or dependencies are needed.

This is an experimental adaptation of the pinned [official single-stage distilled image-to-video workflow](https://github.com/Lightricks/ComfyUI-LTXVideo/blob/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/example_workflows/2.5/LTX-2.5_T2V_I2V_Single_Stage_Distilled.json). Its dimensions and graph connections are valid for the shipped node contracts; it is **not an official native-4K preset qualified for 48 GB GPUs**. INT8 reduces weight storage, but it does not bound attention, image-conditioning, or decoder memory.

| Mode | Rendering path | Qualification |
|---|---|---|
| `image_to_video_native_4k` | Direct 3840×2176 generation → crop to 3840×2160 | Nine frames passed in 73.208 seconds; 121 frames passed in 1041.038 seconds |
| `image_to_video_4k` | 960×544 generation → 1920×1088 latent refinement → 3840×2176 pixel IC-LoRA → crop | Nine- and 33-frame runs passed on the user's 48 GB GPU; [measured results](4k.md) |

## Request and execution

Use [the nine-frame request example](../examples/image_to_video_native_4k.json) or [the experimental 121-frame request](../examples/image_to_video_native_4k_121.json), replacing its placeholder URL with a readable S3/R2 object URL or presigned GET URL. `media.image` also accepts `{"base64":"..."}` or a `data:image/png;base64,...` string. The local tester can upload the image and insert its URL. Keep storage credentials and signed URLs out of committed request files.

The experiment fixes **width 3840, height 2176, and 24 fps**, and accepts **only 9 or 121 frames**, defaulting to nine. Observed duration is **0.375 seconds** for nine frames and **5.041667 seconds** for 121. Other dimensions, frame counts, and FPS values are rejected. The prompt, seed, image strength (0–1), and image compression (0–100) remain configurable; CFG is fixed to 1.0 and the graph uses the shipped eight-step distilled schedule. Do not pass `lora_strength`, `steps`, or `denoise` parameters. The completed runs establish feasibility for their tested settings, without a general memory or execution-time guarantee.

The source image is resized/center-cropped to the aligned generation dimensions. After decoding, `ImageCrop` removes eight rows from the top and eight from the bottom. Tiled VAE decoding uses a 256-pixel tile, 64-pixel overlap, temporal size 16, and temporal overlap 8. These settings reduce decoder pressure; they do not prove that the complete inference fits a particular GPU.

Select **`image_to_video_native_4k`** in the updated [local tester](runpod-tester.md), supply the image, and submit the fixed preset. The tester's default workflow bridge compiles the trusted shipped graph into the deployed worker's existing `workflow` plus `images` API. This uses the **existing Runpod image**, with `ALLOW_CUSTOM_WORKFLOWS=true`; **no new Docker build is needed for the experiment**. Sending the named request directly to the old image is unsupported because that image does not know this new mode name.

When an updated image is eventually deployed, its named API can accept the example directly. The new local Docker build remains blocked by the unavailable Docker Desktop Linux engine. The working bridge and a rebuilt native named API are separate execution paths.

## Admission limits

The updated adapter selects these limits from the trusted manifest's `native_4k` profile:

| Environment variable | Default |
|---|---|
| `MAX_NATIVE_4K_GENERATION_PIXELS` | `8355840` (3840×2176) |
| `MAX_4K_OUTPUT_PIXELS` | `8355840` (aligned render before cropping) |

The first limit is separate from the existing generation limits; the output limit is shared with the previously added 4K modes. The native experiment now allows 121 frames; other profiles' dimensions and frame limits remain unchanged. A caller cannot select the trusted profile by adding an `admission_profile` request field. These are admission checks and do not reserve VRAM. The [manifest](../workflows/manifest.json) and [API graph](../workflows/image_to_video_native_4k.json) contain the exact bindings and defaults. The committed graph still defaults to nine frames; the compiler binds the selected frame count into both video and audio latents without changing that source graph.

For **native 4K at 121 frames only**, the tester includes Runpod per-job `policy.executionTimeout: 3600000` and `policy.ttl: 7200000`, in milliseconds: one hour execution and two hours total TTL. This extends Runpod's default ten-minute execution window for that experiment; it does not change the endpoint or other jobs. [Official Runpod request policies](https://docs.runpod.io/serverless/endpoints/send-requests)

The deployed handler has a separate `WORKFLOW_EXECUTION_TIMEOUT_S` limit, defaulting to **1200 seconds (20 minutes)** unless its Runpod environment overrides it. Extending the outer Runpod policy does **not** override that internal deadline. The first applicable timeout can still end the job. A longer allowed time also provides no guarantee against GPU memory exhaustion.

## Experiment report

**2026-09-12: direct 4K succeeded.** Job `d4e9b5d2-7fd3-4e33-a042-a92a4de5ca0f-e1` reached `COMPLETED`, with worker success and one `s3_url` video. Its ComfyUI prompt ID was `78d8096d-21aa-4a4e-8636-4b1a1d72dbba`. The job used the existing deployed image through the workflow bridge, without a new Docker build. [Structured verification evidence](native-4k-verification.json)

| Measurement | Result |
|---|---|
| Requested frames / FPS | 9 / 24 |
| Diffusion stage | One at 3840×2176; no upscaling |
| Browser video dimensions | 3840×2160 |
| Browser duration | 0.375 seconds |
| Browser readiness | 4; media loaded, no media error |
| Runpod queue time | 11.892 seconds |
| Runpod execution time | 73.208 seconds |
| GPU capacity | 48 GB, confirmed by the user |
| Exact GPU model / peak VRAM | Not measured |

Local checks also passed: **119 worker tests in 49.425 seconds**, **62 tester tests in 1.959 seconds**, and source-based validation of **21 workflows / 55 node classes with zero errors**. The example passed the real `prepare_input()` validator. Graph inspection confirmed the direct aligned-resolution stage and absence of LoRA/upscaler nodes.

This establishes successful direct-4K execution and delivery for the tested nine-frame preset. Browser metadata and readiness were observed; playback progression was **not instrumented**. Peak memory headroom and comparative visual quality were not measured. The newer image's native named API remains untested on Runpod; the successful experiment used the original image's workflow bridge. Signed output URLs and credentials are excluded from the repository.

**121-frame experiment: succeeded.** Job `a69e86cc-1ff6-405c-a82f-707426c1dc1d-e2` reached `COMPLETED`, with worker success and one `s3_url` video. It ran on worker `ul5wtnj5294bpe`, with ComfyUI prompt ID `931795b2-883c-4623-aed8-6c20adc06984`. Only the frame count changed from the successful nine-frame run: the model, image, prompt, seed, dimensions, FPS, and other generation settings stayed the same. The source graph remains unchanged, with SHA256 `96db758e94675e4809cd552d12dda5b2fa8ea9c8a7d16fbb50b5d2930cf7563f`; the compiler binds 121 frames into the video and audio inputs.

| Measurement | 121-frame result |
|---|---|
| Requested frames / FPS | 121 / 24 |
| Diffusion stage | One at 3840×2176; no upscaling |
| Browser video dimensions | 3840×2160 |
| Browser duration | 5.041667 seconds |
| Browser readiness | 4; media loaded, no media error |
| Remote FFprobe | H.264, 3840×2160, 121 frames, 24/1 FPS (reported and average), 5.041667 seconds; exit 0 |
| Runpod queue time | 13.498 seconds |
| Runpod execution time | 1041.038 seconds (17 minutes 21.038 seconds) |
| Playback progression | Not instrumented |
| Exact GPU model / peak VRAM | Not measured |

Local checks for this extension passed **eight native-4K tests** and **62 tester tests in 1.810 seconds**, plus JavaScript syntax and whitespace checks. A remote FFprobe check of the returned 121-frame result independently confirmed its codec, dimensions, frame count, FPS, and duration without saving a local video copy. Both real jobs, browser observations, and FFprobe metadata are recorded in [the verification JSON](native-4k-verification.json), preserving the nine-frame measurements. The allowed frame counts remain **9 or 121**, with nine as the default. These two clip lengths passed on the user's 48 GB worker; other lengths and GPU configurations remain unqualified.
