# LTX 2.5 video upscaling

Use **`video_upscale_x2`** to upscale an existing low-resolution clip using the dedicated **LTX 2.5 Pixel Spatial Upscaler IC-LoRA**. This mode is different from generating a video and running the latent upsampler in a second stage. The default image includes both the special pixel LoRA and the latent spatial upsampler used by other workflows.

The official [2.5 Pixel Spatial Upscaler model card](https://huggingface.co/Lightricks/LTX-2.5-22b-IC-LoRA-Pixel-Spatial-Upscaler) specifies 2× output dimensions, reference downscale factor **2**, and LoRA strength **1.0**, with pre-scaled weights. It directs ComfyUI users to the official IC-LoRA video-to-video workflow. The delivered API graph adapts that topology with the exact 2.5 LoRA, rather than claiming a separately published 2.5 upscaler JSON. [Exact workflow provenance and adaptations](../workflows/PROVENANCE.md)

Included model file:

```text
/comfyui/models/loras/ltx-2.5-22b-ic-lora-pixel-spatial-upscaler-x2-1.0.safetensors
```

Its immutable repository revision, byte size and SHA256 are in [models/manifest.json](../models/manifest.json). The default Docker build downloads and verifies it along with the official 2.5 **Comfy INT8 convrot** distilled transformer and Gemma 4 encoder, BF16 DiffVAE video decoder and audio VAE. The optional `--build-arg MODEL_PROFILE=bf16` build uses the BF16 transformer and encoder with the same upscaler LoRA, decoder and graph. No upscaler model is fetched at startup. The INT8 profile exposes the 21-mode standard catalog and totals 44.54 GB of bundled models.

```json
{
  "input": {
    "mode": "video_upscale_x2",
    "prompt": "Preserve the original subject, composition, and camera motion. Resolve detailed natural textures and clean edges.",
    "media": {
      "video": {"url": "https://YOUR-BUCKET.s3.YOUR-REGION.amazonaws.com/lowres.mp4?YOUR-PRESIGNED-QUERY"}
    },
    "parameters": {
      "width": 512,
      "height": 288,
      "num_frames": 121,
      "fps": 24,
      "seed": 42
    }
  }
}
```

Here, `width` and `height` are the source dimensions after normalization; the result is **1024×576**. Set them to your source clip's dimensions, using multiples of 32. The worker resizes/center-crops if the input differs. It trims the clip to `num_frames`, resamples to `fps`, and rejects clips too short for those settings. This version does not automatically process arbitrarily long videos in segments. Source audio is normalized, used as frozen audio conditioning, and remuxed; silent source videos receive a silent track. Audio is re-encoded, not copied bit-for-bit.

For base64, replace the media object with `{"base64":"..."}` or use `"data:video/mp4;base64,..."`. The helper builds the actual payload from a local clip:

```powershell
python examples/make_request.py video_upscale_x2 --video lowres.mp4 --width 512 --height 288 --frames 121 --fps 24 --prompt "Preserve the original scene and motion while resolving natural fine details." --output upscale-request.json
```

Use S3 for larger video requests and outputs. The job returns through the same Runpod ID, `videos` array, and existing S3 delivery contract as every other mode.

The shipped upscaler uses the fixed eight-step distilled schedule with CFG 1. The LoRA's standard strength is 1.0. See the manifest for any exposed control parameters. There is no claimed 4× LoRA mode, temporal frame interpolation, or full DFR pipeline in this implementation. The DFR pipeline uses a different multi-stage configuration and is not interchangeable with this direct IC-LoRA workflow.

The publisher describes this as creative upscaling for a **clean low-resolution render**. It can synthesize or change fine details; it is not a guaranteed pixel-preserving restoration or artifact-removal model. For INT8, begin qualification on an **RTX 6000 Ada 48 GB with 64 GB+ system RAM**, initially reducing the example to **33 frames**. After it succeeds, test 121 frames and measure peak memory before admitting longer jobs. The BF16 profile's initial recommendation remains an 80 GB GPU with 128 GB+ RAM. These are engineering starting points; neither profile has a measured GPU memory guarantee here.

A 2× change in both dimensions produces four times as many pixels. Quantizing the transformer and encoder does not quantize the VAE or eliminate guide/latent/decoder memory; upscaling may exceed a 24 GB card even if a short single-stage generation fits. The default final-pixel cap is 1920×1088. Raising it requires testing the target GPU, dimensions and frame count; it does not make an insufficient GPU fit.

**Verified here:** the complete INT8 Docker image built successfully with all eleven model files downloaded and SHA256-verified, including this upscaler LoRA. Real startup validated all 18 workflow schemas and model paths. Local media normalization tests and a real base64 image round-trip through the preserved worker delivery path also passed. **Not verified here:** LTX model loading, generated upscale quality, peak inference VRAM, generation runtime, or actual S3 delivery. See [test results](test-results.md) for the build and smoke evidence and the remaining Runpod GPU acceptance tests. The BF16 alternative has not been built or exercised here.
