# LTX 2.5 API

Submit the normal Runpod envelope to `/v2/ENDPOINT_ID/run`, with `input` below. Runpod assigns the envelope `id`; the worker passes it unchanged to the existing S3 uploader. `prompt_id` is the separate ComfyUI execution ID. A top-level Runpod `webhook` remains supported by Runpod itself. The worker does not send a second callback.

The standard `int8`/`bf16`, `cq-v2`, and DFR images expose separate mode catalogs. Send `video_enhance_cq_v2` only to an endpoint running the CQ image; see [the CQ V2 contract](cq-v2.md). The result-delivery envelope remains the same.

The default Docker image uses the official **INT8 transformer and text encoder**. An optional BF16 build accepts the identical 21-mode standard request contract, including `video_upscale_x2` and the three 4K presets. Precision is selected with Docker's `MODEL_PROFILE` build argument, not an input field or generation parameter. Quantized and BF16 images may produce different outputs for the same seed. The bundled VAEs and LoRAs retain their original precision in both profiles.

```json
{
  "input": {
    "mode": "image_to_video",
    "prompt": "A close-up of a red fox slowly turning toward the camera, leaves rustling in the breeze.",
    "parameters": {"width": 768, "height": 512, "num_frames": 121, "fps": 24, "seed": 42},
    "media": {"image": {"url": "https://YOUR-BUCKET.s3.YOUR-REGION.amazonaws.com/fox.png?YOUR-PRESIGNED-QUERY"}}
  }
}
```

Every media role independently accepts `{"base64":"..."}`, `{"url":"https://..."}`, a raw base64 string, or a `data:image/png;base64,...` / `data:video/mp4;base64,...` / `data:audio/wav;base64,...` string. Objects must contain exactly one source key. HTTPS S3 object URLs and presigned URLs retain their original query strings. No input S3 credentials are needed when the URL grants access. Each redirected host is checked; private, loopback, link-local, mixed public/private DNS results, embedded URL credentials, and ports other than 443 are rejected. Optional `INPUT_ALLOWED_HOSTS` restricts exact hostnames further.

Image-to-video using base64:

```json
{"input":{"mode":"image_to_video","prompt":"The camera slowly moves closer as the subject smiles.","media":{"image":{"base64":"REPLACE_WITH_BASE64_IMAGE"}},"parameters":{"num_frames":121,"image_strength":0.7,"seed":42}}}
```

Video-to-video with an S3 object:

```json
{"input":{"mode":"video_to_video","prompt":"Reimagine this movement as a cinematic clay animation, with soft studio lighting.","media":{"video":{"url":"https://YOUR-BUCKET.s3.YOUR-REGION.amazonaws.com/source.mp4?YOUR-PRESIGNED-QUERY"}},"parameters":{"width":768,"height":512,"num_frames":121,"fps":24,"control_strength":1.0,"seed":42}}}
```

Video-to-video with a base64 data URI:

```json
{"input":{"mode":"video_to_video","prompt":"A watercolor animation following the original movement.","media":{"video":"data:video/mp4;base64,REPLACE_WITH_BASE64_MP4"},"parameters":{"num_frames":121,"fps":24}}}
```

First and last frame, mixing base64 and an S3 URL:

```json
{"input":{"mode":"first_last_frame","prompt":"A smooth camera move from the opening scene to the final composition.","media":{"first_frame":{"base64":"REPLACE_WITH_BASE64_FIRST_IMAGE"},"last_frame":{"url":"https://YOUR-BUCKET.s3.YOUR-REGION.amazonaws.com/last.png?YOUR-PRESIGNED-QUERY"}},"parameters":{"width":768,"height":512,"num_frames":121,"fps":24,"first_frame_strength":0.8,"last_frame_strength":0.8,"seed":42}}}
```

CQ Enhancer V2 with a presigned S3 video, sent to the separate `cq-v2` image:

```json
{"input":{"mode":"video_enhance_cq_v2","media":{"video":{"url":"https://YOUR-BUCKET.s3.YOUR-REGION.amazonaws.com/source.mp4?YOUR-PRESIGNED-QUERY"}},"parameters":{"width":1280,"height":704,"num_frames":33,"fps":30,"seed":42,"distilled_lora_strength":0.5,"cq_lora_strength":1.0,"guide_strength":1.0}},"policy":{"executionTimeout":3600000,"ttl":7200000}}
```

The CQ prompt is optional and normally omitted. Base64 and `data:video/...;base64,...` use the same `media.video` alternatives as video-to-video. FPS is fixed at 30 and frame count must be `8n+1` from 9 through 153.

Ready-to-edit CQ request files are [video_enhance_cq_v2_s3.json](../examples/video_enhance_cq_v2_s3.json) and [video_enhance_cq_v2_base64.json](../examples/video_enhance_cq_v2_base64.json). Replace the marked URL or base64 placeholder before submitting them.

Use `python examples/make_request.py` to generate complete payload files with actual base64 from your images/videos. Checked-in JSON examples contain clearly marked S3 placeholders; `image_to_video_base64.json` contains a real, small test image. These examples are test inputs, not evidence of successful model generation.

## Modes

The authoritative list of parameters, defaults, constraints, source references, and node bindings is [workflows/manifest.json](../workflows/manifest.json). API-format graphs are the individual JSON files alongside it. They contain ComfyUI node dictionaries, not editor-format `nodes`/`links` arrays. Submit them through the legacy `workflow` input after setting file names yourself, or let named modes bind everything automatically.

| Mode | Required media | Behavior |
|---|---|---|
| `text_to_video` | none | Single-stage video with generated audio |
| `image_to_video` | `image` | Image-guided video with generated audio |
| `first_last_frame` | `first_frame`, `last_frame` | First/last guides using the official LTX 2.5 template |
| `text_to_video_2stage` | none | Video with a second stage at 2× source width/height |
| `image_to_video_2stage` | `image` | Image guidance and 2× refinement |
| `audio_to_video` | `audio` | Audio-guided video, two stages |
| `image_audio_to_video` | `image`, `audio` | Joint image and audio guidance, two stages |
| `text_to_audio` | none | Audio-only FLAC output |
| `video_to_video` | `video` | Extract Canny edges and guide generation with the official union IC-LoRA |
| `video_to_video_2stage` | `video` | Canny control with 2× refinement |
| `video_control` | `video` | Caller supplies a precomputed depth/pose/edge control video |
| `video_control_2stage` | `video` | Precomputed control video with 2× refinement |
| `video_upscale_x2` | `video` | Dedicated LTX 2.5 Pixel Spatial Upscaler IC-LoRA; 2× source dimensions, preserves audio or inserts silence |
| `video_deblur` | `video` with audio | Deblur IC-LoRA, using the source audio reference |
| `reference_to_video` | `image` | Ingredients/reference-image conditioning |
| `motion_track` | `image` | Image animation guided by `tracks_json` trajectories |
| `video_inpaint` | `video` with audio, `mask_video` | White mask replaces, black retains; two stages and blending |
| `video_outpaint` | `video` with audio | Extend the source canvas using padding; two stages |

`video_to_video` is structural control with an IC-LoRA. It is not a generic `denoise` slider, frame interpolation, or a promise of pixel-identical reconstruction. Unknown modes, media roles, fields, and parameters fail before generation. Use descriptive prompts with actions and desired sounds. Callers must supply a nonempty prompt; graph defaults are editable examples.

Common settings: source width/height are multiples of 32 in 256–1536; half-grid control/motion modes require multiples of 64. `num_frames` is 9–241 and must equal `8n+1`; `fps` is an integer 1–60 accepted by the pinned nodes (24 is the default and the tested transport example). Generation duration is capped at 20 seconds by worker policy. These are admission ranges, not quality or VRAM guarantees. Single-stage default is 768×512; two-stage default is 512×320, producing 1024×640. The dedicated upscaler defaults to 512×288 source, producing 1024×576. `num_frames / fps` controls approximate duration. Seed is an unsigned 64-bit integer. The distilled first stage uses a fixed official eight-step schedule; arbitrary `steps`, `denoise`, and sampler changes are not exposed. CFG is fixed to the mode's documented distilled setting. Negative prompts are accepted but have little/no effect at CFG=1.

For a limited INT8 trial on a free 24 GB GPU, explicitly override the defaults with `{"width":512,"height":288,"num_frames":33,"fps":24}` in a single-stage mode. This is a test starting point, not a measured memory guarantee. CPU offloading also needs enough system RAM. Use the documented 48 GB RTX 6000 Ada / 64 GB+ RAM starting configuration for initial Runpod production qualification, then measure peak memory for each admitted workflow. INT8 does not reduce the output dimensions or frame count automatically.

Mode-specific settings include guide strengths and image compression; control modes expose control strength, and outpaint exposes `pad_left`, `pad_right`, `pad_top`, `pad_bottom` in multiples of 32. Outpaint width/height describe the resized **source** canvas; final dimensions are `2*(width+pad_left+pad_right)` and `2*(height+pad_top+pad_bottom)`. `MAX_OUTPUT_PIXELS` applies after padding/upscaling. Motion-track callers must supply `tracks_json`, a JSON-encoded array of 1–32 tracks, each containing exactly `num_frames` objects `{"x":100,"y":150}` in source-image pixel coordinates. Adjust tracks when changing dimensions or frame count.

## Media normalization and limits

Images: PNG, JPEG, WebP, BMP, TIFF still images; decoded, EXIF-oriented, converted to RGB PNG, stripped of metadata. Animated images are rejected. Default limit 20 MiB and 40 million pixels. Graphs resize and center-crop images to the requested source dimensions.

Videos: standard MP4/MOV with an `ftyp` header, WebM/Matroska, and AVI containers that FFmpeg can decode. Every clip is converted to H.264 MP4, resized/center-cropped, resampled to the requested FPS, and trimmed to the requested number of frames. Audio, when present, is converted to stereo AAC at 48 kHz. A video must decode to at least `num_frames` at the selected FPS; a shorter clip fails instead of silently looping. Supply mask videos aligned with the source, of the same aspect ratio and duration. White/black masks are interpreted through their red channel.

Audio: WAV, FLAC, MP3, Ogg, and supported MP4/M4A containers, converted to stereo 48 kHz PCM WAV. Guided audio must be at least `num_frames/fps` seconds; it is trimmed. Video/audio inputs default to 256 MiB, 120 seconds, and video to 8,847,360 source pixels. Playlist/network-reference formats are rejected. FFmpeg subprocesses have a 180-second deadline. Downloads have byte limits and a 300-second wall deadline with bounded socket reads. A base64 payload is about 4/3 of the file size; Runpod's request body limit may be lower than the worker's media limit, so use S3 for production video.

## Result contract

Illustrative Runpod **COMPLETED** response (not an observed GPU result):

```json
{
  "id": "RUNPOD_JOB_ID",
  "status": "COMPLETED",
  "output": {
    "success": true,
    "prompt_id": "COMFY_PROMPT_ID",
    "videos": [{"filename":"LTX25_00001_.mp4","type":"s3_url","data":"https://YOUR-S3-ENDPOINT/09-26/RUNPOD_JOB_ID/abcd1234.mp4?PRESIGNED-QUERY"}],
    "comfy_credits": {"available": false, "credits_spent": null, "details": []},
    "credit_usage": {"source":"none","credits_per_usd":211.0,"nodes":[],"total_estimated_credits":0.0,"total_estimated_usd":0.0}
  }
}
```

Nonempty `images`, `videos`, `audio`, `files`, and `texts` retain the existing contract; unused arrays are omitted. Binary items use `type: "s3_url"` when S3 is configured or `type: "base64"` otherwise. `comfy_credits` and `credit_usage` retain their existing extraction/estimation logic; they do not represent the Runpod GPU bill. See [the original integration audit](existing-integration.md) for exact S3 bucket behavior and SDK handling.

The worker's validation failure dictionary is:

```json
{"error":"Missing required media input 'last_frame'","error_code":"MISSING_MEDIA"}
```

For cloud job delivery, Runpod SDK 1.7.13 moves `error` to the outer failed result and retains the remaining fields in `output`. An illustrative cloud status response is:

```json
{"id":"RUNPOD_JOB_ID","status":"FAILED","error":"Missing required media input 'last_frame'","output":{"error_code":"MISSING_MEDIA"}}
```

The SDK's **local development `/runsync`** route behaves differently: on failure it returns only `id`, `status`, and `error`, omitting `output` and therefore `error_code`. This was observed against the completed Docker image. Local simulation also assigns a `test-...` job ID; production assigns the Runpod job ID. Do not use the local simulator's error-field omission as the cloud API contract.

Other named-input error codes include `INVALID_INPUT`, `INVALID_URL`, `DOWNLOAD_FAILED`, `INVALID_MEDIA`, `MEDIA_TOO_LARGE`, `UNSUPPORTED_MODE`, `UNSUPPORTED_SETTING`, `MISSING_OUTPUT`, `RESULT_TOO_LARGE`, and `WORKER_CONFIGURATION`. Existing generation/upload failures retain `error`, `details` or `errors`, the prompt ID, and credit fields where available. Partial successfully uploaded artifacts may accompany a **FAILED** job. Any execution or required-upload failure prevents `success:true`. Failed runs after queueing request worker recycling, avoiding the next job overlapping uncertain GPU execution. No whole-generation automatic retry is performed.

Files are namespaced per invocation. Normalized inputs and generated local outputs are removed only after successful artifact delivery; uncertain failures retain files until the failed worker is recycled. S3 partial objects are retained. A caller retry is a new invocation and may generate/upload again; the worker does not implement distributed idempotency storage.

## Existing requests

Requests without `mode` retain the existing `workflow`, named `images`/`videos`/`files`, text overrides, and per-request Comfy.org API-key interface. Existing model-specific raw graphs may require additional models/custom nodes; this image contains the LTX inventory and credit tracker only. Legacy arbitrary graphs and their original upload path require trusted callers. Set `ALLOW_CUSTOM_WORKFLOWS=false` to enforce the validated named-mode API. Do not mix `mode` with `workflow`.

Intentional compatibility fixes: partial failures no longer report success; fake SDK upload paths fail; non-image S3 MIME types are correct; input previews are not returned as generated media; oversized inline outputs require S3. The original worker on D: remains untouched.

## Local verification

The completed INT8 image passed real startup checks for all 18 workflows and a local SDK smoke test: missing-image rejection plus a 32×32 RGB image sent as a base64 data URI through `LoadImage` → `SaveImage`. The returned base64 image retained its dimensions and pixel value; the response included the Runpod ID, ComfyUI `prompt_id`, and `credit_usage`. The report is [artifacts/local-worker-smoke.json](../artifacts/local-worker-smoke.json).

Repeat this limited test with `python examples/smoke_local_worker.py --url http://127.0.0.1:8000 --report artifacts/local-worker-smoke.json`. It expects a local worker started with `SERVE_API_LOCALLY=true`, `ALLOW_CUSTOM_WORKFLOWS=true`, and no S3 configuration. It does not run LTX inference or verify live S3 delivery. See [test results](test-results.md) for the remaining GPU acceptance checks.
