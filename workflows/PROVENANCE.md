# LTX 2.5 workflow provenance and supported modes

## CQ Enhancer V2 profile

The separate `cq-v2` build uses CQdesign's `LTX2.5 - CQ Enhancer lora video workflow V2.json` at repository revision `6495196bc21b2e24615b27e480b30049ca4efeea`. The inspected source graph SHA256 is `7e97e75c6091bdc9eac68b4178f843a3d2a5325f4deb1d4ad5145604b2a4b503`. [Pinned publisher graph](https://huggingface.co/CQdesign/LTX-2.5-CQ-Video-and-Image-Enhancer-LoRAs/blob/6495196bc21b2e24615b27e480b30049ca4efeea/Workflow/LTX2.5%20-%20CQ%20Enhancer%20lora%20video%20workflow%20V2.json)

The worker translation is [video_enhance_cq_v2.json](video_enhance_cq_v2.json). It keeps the model, LoRA order and strengths, convolution VAE, empty prompt conditioning, source-video IC-LoRA guide, sigma schedule, sampling method, 30 FPS requirement, 153-frame cap, and source audio. Worker-side FFmpeg preprocessing replaces the UI video-loader and resize nodes. Core ComfyUI loaders/output nodes replace KJ and Video Helper Suite loaders/output. The bypassed image-conditioning node and unused generated-audio decode are omitted. The detailed mapping and qualification limits are in [the CQ guide](../docs/cq-v2.md).

These are complete ComfyUI **API-format** graphs. They are adapted from the official Lightricks LTX 2.5 examples and the official Comfy-Org first/last-frame template. They contain no UI-only subgraphs. `manifest.json` is the worker's authoritative list of modes, input bindings, defaults, parameter constraints, and output sizing. Regenerate the graphs with `python scripts/generate_workflows.py`.

The implementation was checked against these exact revisions:

| Component | Revision |
| --- | --- |
| [ComfyUI v0.35.0](https://github.com/Comfy-Org/ComfyUI/tree/a7b1d39d342d102f305797fb5ba12dc304d9c1f5) | `a7b1d39d342d102f305797fb5ba12dc304d9c1f5` |
| [ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo/tree/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d) | `15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d` |
| [Comfy-Org workflow templates](https://github.com/Comfy-Org/workflow_templates/tree/cce0b679980e4215000f67fe7b12c3a1982310ea) | `cce0b679980e4215000f67fe7b12c3a1982310ea` |
| [LTX 2.5 model files](https://huggingface.co/Lightricks/LTX-2.5/tree/5e6e71018ee1756ed329b697a7b4aedc934dfce9) | `5e6e71018ee1756ed329b697a7b4aedc934dfce9` |
| [LTX 2.5 pixel spatial upscaler](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler/tree/b29c1f25902886fd40bc43db7b69e942457298d2) | `b29c1f25902886fd40bc43db7b69e942457298d2` |

`source-lock.json` records SHA-256 hashes of the official example files inspected. `../models/manifest.json` pins every included model to its repository revision, size, destination, and SHA-256. The five 2.3-named control adapters in that inventory are explicitly selected in the **2.5** official example graphs; their use is not an assumption of compatibility with older versions.

## Modes

| Worker mode | Required media roles | Sampling | Output behavior |
| --- | --- | --- | --- |
| `text_to_video` | none | 8 steps | Video and generated audio |
| `image_to_video` | `image` | 8 steps | Opening-image conditioning and generated audio |
| `first_last_frame` | `first_frame`, `last_frame` | 8 steps | Both endpoint images guide the clip; generated audio |
| `text_to_video_2stage` | none | 8 + 3 steps | Latent spatial 2x upscale and refinement |
| `image_to_video_2stage` | `image` | 8 + 3 steps | Opening-image conditioning in both stages, 2x output |
| `audio_to_video` | `audio` | 8 + 3 steps | Audio tokens frozen in both stages, original trimmed waveform muxed, 2x output |
| `image_audio_to_video` | `image`, `audio` | 8 + 3 steps | Audio-guided generation with opening image, 2x output |
| `text_to_audio` | none | 8 steps | FLAC audio; video stream disabled |
| `video_to_video` | `video` | 8 steps | Canny structure-guided rerender; new audio |
| `video_to_video_2stage` | `video` | 8 + 3 steps | Same control with latent 2x upscale |
| `video_control` | `video` containing precomputed control frames | 8 steps | Union IC-LoRA for supplied depth, pose, or edge maps; new audio |
| `video_control_2stage` | same | 8 + 3 steps | Union control plus latent 2x upscale |
| `video_deblur` | `video` with audio | 8 steps | Official deblur IC-LoRA; original trimmed audio muxed |
| `reference_to_video` | `image` reference sheet | 8 steps | Ingredients IC-LoRA; new audio |
| `motion_track` | `image`; `tracks_json` parameter | 8 steps | Image plus sparse per-frame motion tracks; new audio |
| `video_inpaint` | `video` with audio, `mask_video` | 8 + 2 steps | Masked-region generation, Laplacian blending, 2x output |
| `video_outpaint` | `video` with audio | 8 + 2 steps | Padded-canvas generation, Laplacian blending, 2x expanded output |
| `video_upscale_x2` | `video` | 8 steps | Official **2.5 pixel spatial IC-LoRA** generates directly at 2x dimensions; source audio retained, silence supplied when absent |

Every media role independently accepts the worker's base64/data-URI or HTTPS S3 input transport. A mask video uses white for pixels to regenerate and black for pixels to retain. Its frames must align with the source clip. Motion tracks use a JSON string containing 1–32 tracks; every track contains `num_frames` points shaped as `{"x": number, "y": number}`, in the resized source canvas's pixel coordinates. The API requires complete trajectories rather than a browser drawing editor.

## Exact graph sources and intentional changes

The [official 2.5 example directory](https://github.com/Lightricks/ComfyUI-LTXVideo/tree/15d09abb5a187a8dcaea2fc31fe51ee96e6c9d0d/example_workflows/2.5) contains the T2V/I2V, A2V, T2A, union-control, V2V, ingredients, motion-track, inpaint, and outpaint recipes. Each manifest entry links to the particular original JSON.

- Hosted `GemmaAPITextEncode` branches, prompt enhancement, preview nodes, UI switches, and UI arithmetic are removed. The baked Gemma 4 12B encoder performs all text encoding locally. No prompt enhancer model, hosted API key, or runtime model download is needed by these graphs.
- All modes default to the official split **INT8 ConvRot distilled** transformer and INT8 ConvRot Gemma 4 encoder. These are the same official filenames selected in the Comfy-Org first/last template; the other source graphs select their BF16 versions. `models/profiles/int8.json` and `models/profiles/bf16.json` preserve both pinned inventories. `python scripts/configure_profile.py --profile bf16 --root .` switches only those two model-loader filenames and the active manifests, and `--profile int8` restores the default. Docker performs this step from `MODEL_PROFILE` at build time; a normal build bakes only the selected profile's weights. The BF16 diffusion video VAE, audio VAE, six IC-LoRAs, and latent upscaler are unchanged. Weights remain in their correct diffusion-model, text-encoder, VAE, LoRA, and latent-upscaler directories. INT8 retains `weight_dtype=default`, allowing ComfyUI to read the file's quantization metadata. INT8 quality, timing, VRAM use, and IC-LoRA execution still require GPU acceptance tests.
- First/last conditioning follows [the actual 2.5 template](https://github.com/Comfy-Org/workflow_templates/blob/cce0b679980e4215000f67fe7b12c3a1982310ea/templates/video_ltx2_5_flf2v.json): two `LTXVAddGuide` nodes with indices `0` and `-1`, dual CFG, `SamplerEulerAncestral` with `eta=0`, and `LTXVCropGuides` after sampling. Endpoint strengths are guidance values; pixel-exact endpoint reproduction is not promised.
- The fixed 8-step distilled sigma schedule is copied from the official 2.5 files. Latent-refinement variants use the official 3-step schedule. Inpaint/outpaint use their separate 2-step Euler refinement. The worker restricts CFG to the distilled model's recommended `1.0`; arbitrary step counts and samplers are intentionally not exposed.
- The official union-control UI graph wires an external depth annotator by default. This worker's `video_to_video` uses the built-in `Canny` node with normalized thresholds, eliminating auxiliary weight downloads. `video_control` accepts caller-precomputed depth/pose/edge frames. Automatic depth and pose estimation are not bundled. Single-stage control omits the optional refinement; the `_2stage` variants retain it. The pinned union README says single-stage while its actual JSON connects a refinement pass; both variants are therefore explicit here.
- The pinned V2V JSON selects the **deblur** adapter. Its README still describes Instant Shave. The JSON and model metadata are the basis of `video_deblur`; Instant Shave is not bundled.
- Outpainting uses core `ImagePadForOutpaint` because the example's `ImagePadForOutpaintTargetSize` is not present in either pinned source tree. The replacement exposes explicit padding in pixels, returns the same image-plus-mask relationship, and repeats the single-frame mask across all source frames before blending. This avoids shortening a video to one frame during mask blending.
- Video inputs are normalized to the requested source resolution/frame rate/frame count by the worker. Graph `ImageFromBatch` nodes bound the frame batch again. Resizing uses center crop. Audio-conditioned editing muxes the original trimmed normalized waveform rather than decoding frozen audio tokens again. These are deliberate input/output adaptations; GPU equivalence with the visual examples has not been measured.

## The requested LTX 2.5 special upscale LoRA

`video_upscale_x2.json` uses `ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors`. The [official 2.5 model card](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler?library=ltx) directs ComfyUI users to add that adapter to an IC-LoRA video-to-video graph. No separate 2.5 pixel-upscaler JSON exists in the pinned example directory, so this graph adapts its 2.5 V2V topology as prescribed by that card.

The recipe loads the adapter at its recommended strength `1.0`, provides the source clip as a guide with reference downscale factor **2**, samples an empty target latent at exactly **twice the source width and height**, removes guide slots, and decodes. For example, `width=512,height=288` produces `1024×576`. This is one generation pass; it does not call `LTXVLatentUpsampler`. The separate `_2stage` generation modes do use that latent upsampler. The worker exposes only the officially published **2x** pixel adapter, not an assumed 4x 2.5 variant. Full DFR temporal sampling and duration prediction are not claimed by this implementation.

This upscaler creates plausible fine detail. Use clean, low-resolution generated clips and a prompt describing their content. It is not a forensic restoration mode or a guarantee of exact preservation of every source detail. Its direct ComfyUI recipe uses strength `1.0`; the `0.5` setting documented for the separate DFR pipeline is not substituted here.

## Parameters and dimensions

`num_frames` is `8n+1` (9–241), default 121. `fps` defaults to 24; its native schema permits 1–60 in these wrappers, with the worker also applying its duration/admission limits. Reproducibility requires a fixed `seed`, unchanged weights, identical inputs, and matching software/hardware; cross-GPU bitwise identity is not guaranteed.

Most single-stage graphs default to `768×512`. Two-stage graphs default to a `512×320` source/initial stage, resulting in `1024×640`. Dimensions are multiples of 32; union control and motion tracks require multiples of **64** because their reference grid is half resolution. `video_upscale_x2` defaults to `512×288`, samples at `1024×576`, and requires source dimensions divisible by 32. Its target latent bindings carry a `multiplier: 2`; ordinary resize bindings use the unchanged source dimensions.

For outpaint, width/height specify the source canvas after normalization. Final dimensions are `2 × (width + pad_left + pad_right)` and `2 × (height + pad_top + pad_bottom)`. The worker limits expanded output pixel count before generation. These limits are admission limits, not guarantees of available VRAM.

## Verification status

`static-validation.json`: all **18** API graphs passed graph-link, cycle, binding, model-inventory, exact node-class, required-input, numeric-range, and output-slot checks against the pinned source. The report identifies the source file and line for all **54** distinct classes. This check does not import PyTorch model code or load weights.

`dynamic-schema-validation.json`: all save nodes passed an isolated execution of the pinned ComfyUI DynamicCombo schema/argument expansion code. `SaveVideo` inputs expand to MP4/H.264 arguments, and `SaveAudioAdvanced` expands to FLAC. This specifically checks modern dotted `format.codec` inputs. It does not run codecs or start ComfyUI.

`python scripts/validate_workflows.py --object-info http://127.0.0.1:8188/object_info --models-present --model-directory /comfyui/models` checks registered runtime classes and installed model paths inside the built image. Template input filenames are deliberately not checked for membership until actual job media is staged. Model generation, visual quality, GPU memory consumption, and timings still require a Runpod GPU test. No generation success is claimed by either report.

The original Lightricks license is retained in `LICENSE-LTX-2-Community.txt`; it applies to those upstream examples and their adaptations. Model licenses are independently identified by their pinned repositories.
