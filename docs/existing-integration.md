# Existing worker integration audit

Audited `D:\worker-comfyui` before making implementation changes. The original
checkout was at commit `114da7fa3ceaa7113468ac43ed7f4f53c71674f3`, with existing
uncommitted Dockerfile/docker-bake changes and untracked local files. The new
implementation lives in the separate `D:\worker-comfyui-LTX-2.5` working copy;
the original checkout and its changes were not modified.

Read: `AGENTS.md`, `handler.py`, `credit_estimator.py`, `src/start.sh`, Dockerfile,
`tests/test_handler.py`, the webapp caller, and existing configuration docs.

## Job flow and identifiers

The Runpod Python SDK owns receiving jobs and submitting completed/failed results
to Runpod. The original handler receives an envelope containing `id` and `input`.
It validates `input.workflow`, optionally applies prompt overrides, waits for the
local ComfyUI HTTP API, uploads named inputs through `/upload/image`, opens a
websocket using a generated client ID, and posts the API graph to `/prompt`.

ComfyUI returns a separate `prompt_id`. The handler filters websocket events by
that ID, handles execution errors and interruptions, polls `/history/{prompt_id}`
as a completion fallback, and waits briefly for history persistence. Output bytes
are fetched through ComfyUI's local `/view` endpoint.

The **original Runpod job `id`**, not the ComfyUI `prompt_id`, is the S3 key prefix
and the caller's `/status/{id}` identifier. The adapter retains this distinction.
The SDK attaches the original job identity to its completion response; the
handler does not invent a replacement job ID.

There is **no custom callback or webhook HTTP implementation** in the original
worker. An outer `webhook` field on a Runpod request is handled by Runpod. The
worker continues to return through the SDK and does not independently send a
duplicate notification. The original webapp uses `runsync` and then polls
`status` while the Runpod envelope is queued/in progress. Its proxy forwards the
request payload and its renderer reads `output.videos` and compatible media
items; no change to that result shape is required.

## Existing result contract

A successful handler result contains:

```json
{
  "success": true,
  "prompt_id": "comfy-prompt-id",
  "videos": [
    {
      "filename": "generated_00001_.mp4",
      "media_type": "video",
      "type": "s3_url",
      "data": "https://storage.example/result.mp4?presigned-parameters"
    }
  ],
  "comfy_credits": {"available": false, "credits_spent": null, "details": []},
  "credit_usage": {
    "total_estimated_credits": 0.0,
    "total_estimated_usd": 0.0,
    "credits_per_usd": 211.0,
    "source": "none",
    "nodes": [],
    "prompt_id": "comfy-prompt-id"
  }
}
```

`images`, `videos`, `audio`, `files`, and `texts` are included when nonempty.
Media entries retain `filename`, `media_type`, `type`, `data`, and any output
`format`/`frame_rate` metadata provided by a node. With no S3 endpoint configured,
binary outputs use `type: "base64"`; video entries include the existing warning
about returning base64. Text artifacts retain inline `texts` plus S3 `files`
when storage is configured.

To avoid oversized Runpod responses, each inline binary artifact is now capped
by `MAX_INLINE_OUTPUT_BYTES` (default **5 MiB before base64 encoding**; zero
disables inline binary delivery). Larger files fail with an explicit instruction
to configure S3. This is an intentional compatibility change for large inline
outputs. S3 uploads are not constrained by this inline cap. Named-mode responses
also undergo an aggregate JSON-size check in the adapter.

Runpod wraps successful output in its own envelope, normally including `id`,
`status: "COMPLETED"`, and `output`. These examples describe the schema and are
not evidence of a GPU execution or a real upload.

## Storage compatibility and corrected MIME types

The same runtime secret names remain in use:

| Variable | Behavior |
| --- | --- |
| `BUCKET_ENDPOINT_URL` | Enables S3 output uploads when set. |
| `BUCKET_ACCESS_KEY_ID` | Access key for the configured S3 endpoint. |
| `BUCKET_SECRET_ACCESS_KEY` | Secret key for the configured S3 endpoint. |

Both Runpod upload helpers in pinned SDK **1.7.13** default to a bucket named for the
current month/year (`MM-YY`) when no explicit bucket is supplied. The original
worker did not supply a bucket name. This implementation preserves that behavior
and does not silently reinterpret the endpoint URL as a new bucket-name setting.
The deployment must provide an endpoint and permissions compatible with this
existing convention, including access to the month bucket at calendar rollover.

Still images retain `rp_upload.upload_image`. Video, audio and other files use
`rp_upload.upload_file_to_bucket` from the same SDK, preserving the original
job-ID key prefix, randomized eight-character filenames, month-bucket default,
and seven-day presigned result URL. The file helper supplies the proper MIME
type (`video/mp4`, for example); the legacy image helper would have labeled MP4
as `image/mp4`. File extensions and the caller-facing generated filename remain
unchanged. Uploads are synchronous: the handler waits for successful completion.

Partial S3 credentials now fail before generation. Returned URLs must be usable
HTTP(S) URLs. This catches the SDK's local simulated-upload fallback instead of
reporting a local path as an S3 result. Signed result URLs are no longer printed
by the worker's upload log.

## Credit reporting

The custom credit integration is retained. The handler extracts runtime credit
metadata from ComfyUI history, then queries the existing credit-tracker SQLite
database by **ComfyUI prompt ID**. If matching tracker rows exist, they take
precedence; otherwise `credit_estimator.py` estimates supported paid Comfy API
nodes and prefers runtime prices where available.

`CREDIT_TRACKER_DB_PATH` keeps its original default:
`/comfyui/custom_nodes/comfyui_credit_tracker/usage_log.db`. The existing
`COMFY_ORG_API_KEY` and per-request `comfy_org_api_key`/`api_key_comfy_org` inputs
remain supported for compatible raw API graphs. The per-request key overrides
the environment and is passed in ComfyUI's `extra_data.api_key_comfy_org`.

Local LTX inference does not consume the paid Comfy API-node credits tracked by
this estimator. Zero reported Comfy credits are **not** zero Runpod GPU cost;
Runpod bills compute separately. The credit fields are retained for caller
compatibility and are not a Runpod billing estimate.

## Necessary error and lifecycle corrections

The original handler could return `success: true` when one output existed even
though another upload or workflow node had failed. Text-upload failures were
silently downgraded to inline text. Both behaviors are corrected: execution,
output-read, and required-upload failures now return an `error`, with
`success: false` for failures finalized after collecting artifacts. Partial
artifacts and credit details remain available for diagnosis. Persisted history
errors take precedence over ComfyUI's legacy `executing(node=None)` sentinel.

The SDK removes the handler's `error` key from `output` and places it in the
Runpod error result. It similarly consumes `refresh_worker: true` into its
worker-stop control. Consumers should check the outer Runpod failure status and
error, and may inspect `output.details`, `output.errors`, partial media arrays,
`output.prompt_id`, and credit fields when supplied. Do not interpret partial
media on a failed job as completed generation.

Timeouts request ComfyUI interruption. Failures after queue submission request a
worker refresh, including an ambiguous queue HTTP timeout where the server may
have accepted the prompt without returning its ID. `REFRESH_WORKER=true` now
also activates the documented refresh behavior after successful delivery.
Closing an already-disconnected websocket cannot overwrite a completed result.

Temporary upload copies remain present until the synchronous upload finishes and
are removed in `finally`. Original ComfyUI output files are not deleted by the
delivery helper. The LTX adapter performs scoped cleanup after successful output
serialization/upload; on generation or delivery failure it retains job files
until the worker is replaced, avoiding deletion beneath a still-running prompt.

Core `SaveVideo` returns its file under `images` with `animated: [true]`; the
handler correctly classifies the MP4 as a video and treats `animated` as a UI
hint. Input and temporary preview descriptors are skipped, so `LoadVideo`
previews cannot be returned as generated output. Duplicate descriptors are
deduplicated before upload. Core audio output descriptors remain supported.

## Compatibility boundary

Named LTX modes use the new validated media adapter and call
`handler(job, scan_text_artifacts=False)`. This disables the legacy global
recent-text-file scan while keeping history-declared/inline text processing.
It prevents another recent job's text files from being attached to a named-mode
result. The raw workflow route retains the original default for compatibility.

Raw ComfyUI API graphs remain a **trusted caller** interface: the original media
upload path permits HTTP(S) URLs and permissive base64 without the named-mode
download bounds/SSRF controls. Arbitrary node graphs also expose whatever
capabilities the installed nodes provide. Use authenticated callers and the
documented custom-workflow switch when deploying a named-mode-only endpoint.
The new named-mode validation does not make arbitrary legacy graphs safe for
untrusted public use.

## Verification evidence

The original tests and added delivery regressions run locally with mocked
ComfyUI/S3 interfaces. They cover original response behavior, prompt identity,
paid-credit mapping, partial delivery failure, timeout/ambiguous queue failure,
history failure precedence, correct video MIME/job prefix, upload-temp lifetime,
rejection of simulated S3 URLs, core video/audio schemas, preview filtering,
deduplication, and scoped text-discovery control. See the project test report for
the final count. No live S3 upload, Runpod webhook, or GPU generation was executed
by these unit tests.

SDK behavior was checked against the original 1.7.12 source and then against
`rp_upload.py`, `rp_job.py`, and `rp_http.py` extracted from the exact official
[Runpod 1.7.13 wheel](https://pypi.org/project/runpod/1.7.13/#files) used by the new
image lock. Its SHA256 is
`033ae142027d36f0c1db95103ef6d0b23fd9ec875fd8dba5154e3c519d3bcf70`, matching
`requirements/runtime.lock`. The reviewed upload arguments, month-bucket and
job-key conventions, URL lifetime, result/error handling, and refresh controls
are unchanged between those versions. This verifies the shipped package rather
than assuming a GitHub tag exists for the release.
Video/audio history shapes were checked against the pinned ComfyUI source in
`comfy_extras/nodes_video.py` and `comfy_api/latest/_ui.py`.
