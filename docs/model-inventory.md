# Complete model inventory

The standard INT8 and BF16 images bundle eleven files. **INT8 is the default**; use `--build-arg MODEL_PROFILE=bf16` for the larger BF16 image. The separate `cq-v2` profile bundles the six files used by the CQ Enhancer V2 video workflow, including the dev INT8 transformer, distilled LoRA, CQ V2 LoRA, and convolution VAE.

| Build profile | Bundled files | Exact bytes | Decimal GB | GiB |
| --- | ---: | ---: | ---: | ---: |
| `int8` | 11 | 44,542,597,655 | 44.54 | 41.48 |
| `bf16` | 11 | 75,947,642,823 | 75.95 | 70.73 |
| `cq-v2` | 6 | 48,934,613,516 | 48.93 | 45.57 |

Paths below are relative to `/comfyui/models/`. Repository revisions and LFS SHA256 values are immutable pins, and downloads verify the exact byte count and hash before completing the build. The active default manifest is [models/manifest.json](../models/manifest.json); profile sources are [int8.json](../models/profiles/int8.json), [bf16.json](../models/profiles/bf16.json), and [cq-v2.json](../models/profiles/cq-v2.json). The image uses only its selected profile and cannot switch through a generation request or runtime variable.

## CQ Enhancer V2 bundle

The exact six-file CQ inventory and API-specific compatibility notes are in [the CQ V2 guide](cq-v2.md). Its 48.93 GB total is larger than the standard INT8 model total because it uses the dev transformer plus the 8.90 GB distilled LoRA. It does not include the standard generation IC-LoRAs, pixel/latent upscalers, or DiffVAE because the CQ V2 graph does not reference them.

**Complete INT8 build verified:** the Docker build exited **0** and successfully exported and loaded **`worker-comfyui-ltx25:int8`**. All eleven files below were downloaded, and every exact byte count and SHA256 passed, including the dedicated LTX 2.5 pixel upscaler LoRA. The verified model total is **44,542,597,655 bytes**. Each file succeeded on its first download attempt; the model stage took **2,898.9 seconds (about 48 minutes 19 seconds)**. Docker reported **1,179.0 seconds** for export and **272.5 seconds** for unpacking.

Docker's **`Size` field reports 42,222,685,974 bytes (42.22 GB)**. The running container's **apparent root filesystem size is 52,001,502,862 bytes (52.00 GB)**. These measurements are distinct from the model-byte total and from free build-space requirements. Full worker startup passed the eleven-file check, a real CUDA kernel, all 18 workflow runtime schemas, 1,003 registered nodes, and credit hooks. A real 32×32 data-URI image also passed through `LoadImage` → `SaveImage` and the local Runpod API response contract. **LTX inference, live S3 delivery, and cloud callbacks have not been tested.**

## Default INT8 bundle

| Destination | Bytes | Official source at pinned revision | SHA256 |
| --- | ---: | --- | --- |
| `vae/ltx-2.5-audio-vae-bf16.safetensors` | 364,866,540 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/vae/ltx-2.5-audio-vae-bf16.safetensors) | `c52733d37f6a7fb7949c3dc0fb468c6cb2169e4d836983a73babb9f0d54837a5` |
| `vae/ltx-2.5-video-vae-bf16.safetensors` | 1,472,223,346 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/vae/ltx-2.5-video-vae-bf16.safetensors) | `847e14ca7f3355debca0cea4eaa24ac0fbcdf0061da054ac89ca638a869ddba3` |
| `diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors` | 21,504,034,224 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors) | `c4279eeff115cbeaca494bd2183e7d768c38fe85a184dc6afbb7159157c44334` |
| `latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors` | 995,778,752 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/latent_upscale_models/ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors) | `eb5a71fe4068ee87ccdb1c3aa635e547ca76bd2d30ae20ae889f2c325c0677e8` |
| `text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors` | 15,372,969,374 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors) | `6ce688a0aa98a5fa36a9f1e6c3f42152a498cc2b53ee8c15674c64244f91487f` |
| `loras/ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors` | 1,308,778,338 | [Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients@dcc403d92aa0f5a983c5dbc7282503ca52f73a08](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Ingredients/blob/dcc403d92aa0f5a983c5dbc7282503ca52f73a08/ltx-2.3-22b-ic-lora-ingredients-0.9.safetensors) | `515e4e139001ac6282357a5b35372e42e98b3affd5fcc886a52242abeed19559` |
| `loras/ltx-2.3-22b-ic-lora-in-outpainting-0.9.safetensors` | 1,308,778,338 | [Lightricks/LTX-2.3-22b-IC-LoRA-In-Outpainting@554060ba18950e5fd5e91e6a9ab2f55852fd05a4](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-In-Outpainting/blob/554060ba18950e5fd5e91e6a9ab2f55852fd05a4/ltx-2.3-22b-ic-lora-in-outpainting-0.9.safetensors) | `73dd0841c0d4f0eb26fb1f017781b841b2752021944ac5ecefe57917f6dae6b5` |
| `loras/ltx-2.3-22b-ic-lora-motion-track-control-ref0.5.safetensors` | 327,309,314 | [Lightricks/LTX-2.3-22b-IC-LoRA-Motion-Track-Control@572bb9c9a1ba3d8e8724cce69783ffc2422386db](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Motion-Track-Control/blob/572bb9c9a1ba3d8e8724cce69783ffc2422386db/ltx-2.3-22b-ic-lora-motion-track-control-ref0.5.safetensors) | `e279807ee3aa3db1ce60188d665ff83342860367dcd6bac19f8bd5a99a9e1dca` |
| `loras/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors` | 654,465,352 | [Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control@b4d1c4d8c9e544e9bbbd6811bb4363708b6093ff](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Union-Control/blob/b4d1c4d8c9e544e9bbbd6811bb4363708b6093ff/ltx-2.3-22b-ic-lora-union-control-ref0.5.safetensors) | `a1b888a87f661d27f08b394ae559e8e1050be33900bcc36a5cdf659e48f88d18` |
| `loras/ltx-2.3-22b-ic-lora-deblur-0.9.safetensors` | 906,071,437 | [Lightricks/LTX-2.3-22b-IC-LoRA-Deblur@4b10fd3b154ae0119936e9d3f922d32a2f0a9422](https://huggingface.co/Lightricks/LTX-2.3-22b-IC-LoRA-Deblur/blob/4b10fd3b154ae0119936e9d3f922d32a2f0a9422/ltx-2.3-22b-ic-lora-deblur-0.9.safetensors) | `dcdd73b57c2c4d5f5bc6535e825f4758b654a583bc991caa50c6809b6990b4ab` |
| `loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors` | 327,322,640 | [Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler@b29c1f25902886fd40bc43db7b69e942457298d2](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler/blob/b29c1f25902886fd40bc43db7b69e942457298d2/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors) | `984851b769ea2bcb4c9e0a239a7676239e42c6a6001ddc69943b41ff0b283c1d` |

## Optional BF16 replacements

The BF16 profile substitutes these two files for the corresponding INT8 files above. The other nine records are identical. It does not bundle both transformer/encoder variants. The two BF16 replacements have pinned publisher metadata but were not downloaded in the INT8 build; the BF16 model image has not been built.

| Destination | Bytes | Official source at pinned revision | SHA256 |
| --- | ---: | --- | --- |
| `diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors` | 42,018,190,584 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/diffusion_models/ltx-2.5-22b-distilled-transformer-bf16.safetensors) | `31eb3cad89b9e54e99dd3baf286f70825ac4f6c660a70d9184d895be76d7bff4` |
| `text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors` | 26,263,858,182 | [Lightricks/LTX-2.5@5e6e71018ee1756ed329b697a7b4aedc934dfce9](https://huggingface.co/Lightricks/LTX-2.5/blob/5e6e71018ee1756ed329b697a7b4aedc934dfce9/text_encoders/gemma4-12b-with-proj-ltx-2.5-bf16.safetensors) | `ef7243612fdae7a75cb4d5cee9433e81380675fb6c213bd98ae74a9cd16561d1` |

The audio VAE contains its vocoder. Both Gemma4 encoder variants include the tokenizer and LTX projection layers. Native ComfyUI loaders read the official Comfy INT8 convrot quantization metadata; no additional quantization node pack or runtime conversion/download is required. The workflow loader setting remains `weight_dtype: default`.

No auxiliary Canny weights are required. All five selected 2.3 IC-LoRAs are referenced by the publisher's pinned 2.5 example workflows. The additional 2.5 pixel upscaler LoRA follows its official model card and powers `video_upscale_x2`. See [build research](build-research.md) for source pins, omitted optional components, hardware estimates and verification limits.

File sizes in this inventory are model bytes, not Docker build-space or VRAM requirements. Plan 150-200 GB free build storage for INT8 and 250-300 GB for BF16. Building needs no GPU; generation memory depends on the workflow, resolution, frame count, conditioning, decoder and CPU offloading.
