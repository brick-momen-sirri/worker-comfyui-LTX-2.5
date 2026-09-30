# Local Runpod tester

Use this page to send jobs to your deployed LTX 2.5 endpoint, upload source media to S3, inspect endpoint health, and view returned media. It runs locally with **Python 3.10 or newer**. The core tester uses the standard library; S3 source uploads additionally use the pinned Boto3 dependency. The `comfyui`, `cq-v2`, and `dfr` runtime selections each target their matching separately deployed image.

## Launch

In Windows PowerShell:

```powershell
cd D:\worker-comfyui-LTX-2.5
python -m pip install -r tools/runpod_tester/requirements.txt
python tools/runpod_tester/server.py --endpoint dfadob3rm5dg32 --open
```

In WSL:

```bash
cd /mnt/d/worker-comfyui-LTX-2.5
python3 -m pip install -r tools/runpod_tester/requirements.txt
python3 tools/runpod_tester/server.py --endpoint dfadob3rm5dg32
```

Then open [http://localhost:8766](http://localhost:8766) in your Windows browser. Keep the terminal running; **Ctrl+C** stops the local server. Use `--port 8767` if the default port is occupied, and open that port instead.

Install dependencies into the same Python environment that launches the tester. `boto3==1.35.31` is a local tester dependency; it does not modify the worker image. Existing health, job, base64, and supplied-URL features still work without it. Attempting an S3 upload without Boto3 displays an installation error.

The endpoint can be changed in the page, passed through `--endpoint`, or supplied by `RUNPOD_ENDPOINT_ID`. Enter your **Runpod API key** in the password field. Alternatively, provide `RUNPOD_API_KEY` privately in the server process's environment before launch; the page then uses that configured key when its password field is empty. No key value is printed, written to the repository, or saved in browser storage. Reloading the page clears a key entered there.

The optional `--keep-key-in-memory` launch flag retains the API key in the local server after a successful **Check endpoint** call, so another test tab or a page reload can reuse it. It is disabled by default. Stop the server to clear this retained key; it is never written to disk.

## Select the worker runtime

`--runtime comfyui` is the default. It exposes the existing ComfyUI modes, including the Comfy upscale presets and the experimental direct-4K preset. Its catalog does not include Python DFR modes.

For the separate CQ Enhancer image:

```powershell
python tools/runpod_tester/server.py --runtime cq-v2 --endpoint YOUR_CQ_ENDPOINT_ID --port 8767 --open
```

This catalog exposes only `video_enhance_cq_v2`. It uses 1280×704, 30 FPS, and 33 frames for the first paid test; the publisher-aligned maximum/default in the worker contract is 153 frames. Prompt fields and the unrelated validation-only preset are hidden. Each CQ request receives a one-hour execution timeout and two-hour TTL. The source video can be selected locally, uploaded to S3 by the page, or supplied as an HTTPS URL.

After deploying the separate DFR worker image, start another tester against that endpoint, for example:

```powershell
python tools/runpod_tester/server.py --runtime dfr --endpoint YOUR_DFR_ENDPOINT_ID --port 8767 --open
```

The runtime is selected when the local server starts. A browser request cannot override it. Each catalog rejects modes from the other images before contacting Runpod. Runtime selection is an operator choice: the tester cannot inspect the remote image. Enter the endpoint running the matching image; a successful health check does not verify image compatibility.

DFR submits a validated **named-mode request**, without the ComfyUI legacy graph bridge. Its preset is **33 frames at 24 FPS**, with 9 and 121 frames also accepted. Spatial stages are fixed at **960×544 → 1920×1088 → 3840×2176**, followed by a center crop to **3840×2160**; temporal upscaling is off. Prompt and seed remain configurable; image-to-video also exposes `image_strength`, default **0.8**. Negative prompts are not accepted by this DFR contract, and the ComfyUI validation-only button is hidden. These are admission settings, not a guarantee that every frame count fits a particular GPU.

Each DFR job receives a **5,400,000 ms execution timeout (90 minutes)** and **7,200,000 ms TTL (two hours)**. The worker's separate 60-minute pipeline timeout still applies. The request budget also allows the default five-minute input download, three media commands of up to three minutes each, and time for output delivery. Existing API-key handling, S3 source uploads, cancellation, status polling, and result display remain available. The local DFR tester does not need Torch, Pillow, or the Python model pipeline installed; those dependencies belong to the DFR worker image.

The existing **Comfy upscale 4K** chain and **experimental direct 4K** preset are separate from official DFR. The direct 121-frame job completed, but the user reported color artifacts. Execution and delivery passed; visual quality was not established and the artifact cause has not been diagnosed. The DFR path follows the staged implementation described in the [official pipeline documentation](https://github.com/Lightricks/LTX-2/blob/main/packages/ltx-pipelines/docs/pipelines.md#12-dfrpipeline).

## First test

For the default ComfyUI runtime, the **Comfy upscale 4K** presets use fixed dimensions and begin with nine frames. See [the 4K guide](4k.md) for the existing-image compatibility transport, updated-image named API, and actual qualification results. The historical 18-mode checks below predate the 4K additions.

1. Click **Check endpoint**. This checks API access and worker/queue counts. It does not execute LTX inference.
2. Select a generation mode and describe the scene. The ComfyUI page loads its modes and settings from [the workflow manifest](../workflows/manifest.json); the DFR page loads the separate DFR validator's mode specifications.
3. Start with the small preset, then click **Run test**. This submits one real job to Runpod and can incur GPU charges.
4. Watch the job ID, status, queue/execution timing, and returned media. The page checks status every five seconds; **Check status** also requests an update.

| Initial test | Dimensions | Frames / FPS | Required media |
| --- | --- | --- | --- |
| Text to video | 512×320 | 9 / 24 | None |
| Image to video | 512×320 | 9 / 24 | Opening image |
| Video upscale 2× | 256×256 source → 512×512 output | 9 / 24 | Source video |

These are short functional tests, not quality benchmarks or measured memory guarantees. Try 33 frames after the small job succeeds. Source video/audio must cover the requested duration; a one-second clip is sufficient for the nine-frame upscaling preset. The optional **Validation-only preset** intentionally omits an image: its expected failure tests error delivery, not generation.

## Inputs and results

Each media role independently accepts a local file or HTTPS URL, including S3 presigned URLs. Files sent directly with a job become base64 data URIs in memory. First and last frames can use different transports. Direct base64 inputs are limited to **6 MiB per file** and **9,000,000 bytes for the complete outgoing JSON**; base64 increases size by approximately one third. Use **Upload to S3** or a supplied S3 URL for larger videos or combined inputs. Runpod documents a 10 MB limit for asynchronous `/run` requests. [Official operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference)

The page previews the existing worker's `images`, `videos`, and `audio` results, including `type: "s3_url"` and `type: "base64"`. It also exposes the job ID, timing, and response JSON for diagnosis. The on-page JSON hides media bodies and signed URL queries; **Download full response** saves the full returned data when requested. That downloaded report can contain media and private signed result URLs.

**Cancel job** calls Runpod's cancellation operation for the current job. **Detach**, closing the page, or stopping the local server only stops watching; the cloud job can continue. Save the job ID before leaving. The tester uses `POST /run`, `GET /status/{job_id}`, `GET /health`, and `POST /cancel/{job_id}` against Runpod's queue-based API. [Official operation reference](https://docs.runpod.io/serverless/endpoints/operation-reference)

## Upload source media to S3

Enter the following in the page's S3 settings, using an existing bucket:

| Setting | Value |
| --- | --- |
| Endpoint URL | Your HTTPS S3 service endpoint, such as `https://s3.eu-central-1.amazonaws.com`; enter the bucket separately |
| Region | The bucket/provider's region |
| Bucket | The existing bucket name |
| Access key / secret key | Credentials allowed to upload and read objects in the selected prefix |
| Session token | Include it when using temporary credentials; otherwise leave it empty |
| Object prefix | Defaults to `ltx-inputs` |
| URL expiry | Select **1, 6, or 24 hours**; defaults to **24 hours**. Allow enough time for queueing, cold start, and input download |
| Addressing style | Defaults to **Path**; select **Auto** or **Virtual host** if required by your provider |

For **Cloudflare R2**, use the service endpoint `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`, with no `/bucket` or object path. Put the bucket name only in **Bucket** and set **Region** to **`auto`**. `WEUR` is a storage-location hint, not the S3 signing region. Copy any jurisdiction-specific service hostname exactly as supplied by Cloudflare. [Cloudflare S3 setup](https://developers.cloudflare.com/r2/get-started/s3/), [R2 data locations](https://developers.cloudflare.com/r2/reference/data-location/)

Choose a file for its media role, then click **Upload to S3**. The local server uploads the file with a unique object name under the chosen prefix, creates a presigned **GET** URL, and verifies that URL is readable before reporting success. The URL automatically fills that role's HTTPS input. Repeat independently for each input, including first and last frames. This step prepares source media; **Run test** submits the generation afterward. Boto3 creates the signed PUT and GET URLs; the tester's own bounded HTTPS transport performs the upload and readback verification. [Boto3 presigned URL support](https://boto3.amazonaws.com/v1/documentation/api/1.35.31/guide/s3-presigned-urls.html)

S3 uploads accept **images up to 20 MiB** and **video/audio up to 256 MiB**. They travel through the local server, so browser-to-S3 upload CORS rules are not required. The bucket's service and resulting HTTPS URL must be reachable from your computer and the Runpod worker. Input decoding, duration, and workflow requirements still apply when generation begins.

S3 credentials stay in memory and are not written to the repository or browser storage. The tester creates no bucket and applies no public ACL. The credential principal needs **`s3:PutObject` and `s3:GetObject`** for the selected objects. Presigned access retains that principal's restrictions. Temporary credentials can expire before the requested URL lifetime; an expired session makes its URLs stop working even when the selected expiry is longer. [AWS presigned URL permissions and expiration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html)

The local temporary upload copy is removed after the upload request finishes. Uploaded S3 objects remain in your bucket: clearing a role, changing modes, or closing the page does not delete them. Manage their retention with your bucket's lifecycle policy or your usual cleanup process. Re-uploading creates a new unique object rather than replacing a previous input.

These page settings control **source input uploads**. Generated-output S3 delivery is still configured separately on the Runpod worker through `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY`, and optional `BUCKET_REGION`. Entering S3 settings here does not change those runtime secrets. See [the worker API contract](API.md).

## Reading status and failures

- **R2 upload connection reset / WinError 10054:** verify that the endpoint contains only the service hostname, the bucket is separate, and Region is `auto`. Restart the local tester and reload the page after updating its files. The corrected page validates settings before sending the file, so malformed endpoint/region settings produce a specific error without transferring the binary body.
- **401 / 403:** check the API key and its permission to access this private endpoint.
- **IN_QUEUE:** waiting for a worker; a first job may include a cold start or image pull. Inspect endpoint health and Runpod worker logs if it persists.
- **IN_PROGRESS / RUNNING:** processing. These are active states, not completion.
- **FAILED:** inspect the actual outer error, worker error code, details, and logs. Partial outputs can accompany a failed job.
- **CANCELLED / TIMED_OUT:** terminal states. A completed status with a worker error is not a successful generation.
- **404:** check the endpoint/job ID; an expired job or result may no longer be available.
- **Submission uncertain:** a timeout can occur after Runpod accepted a job. Check the Runpod dashboard before submitting again; the tester never automatically resubmits a generation.

Runpod lists the supported [job states](https://docs.runpod.io/serverless/endpoints/job-states). Async results are retained for 30 minutes after completion; job TTL and execution timeout are separate limits. The tester does not change endpoint settings. It supplies per-request policies for CQ, DFR, and the 121-frame experimental direct-4K job; other presets inherit the endpoint's policy. [Official request policies](https://docs.runpod.io/serverless/endpoints/send-requests)

Queue and execution timing values returned by Runpod are milliseconds; the page presents readable timings. [Official ComfyUI API tutorial](https://docs.runpod.io/tutorials/serverless/comfyui)

## Verification

**DFR tester integration, local checks:** **73 tester tests passed in 2.767 seconds**, including 11 new tests for runtime catalog isolation, wrong-runtime rejection before cloud submission, named DFR payloads and policies, configuration without model/image dependencies, media transport preservation, and timeout redaction without retries. The HTTP cases used temporary loopback servers and every Runpod request was mocked. JavaScript syntax and whitespace checks passed. These checks did not launch or restart the user's tester, submit a cloud job, build a DFR image, run DFR inference, or validate DFR output quality.

**Direct 4K follow-up on 2026-09-12:** the experimental direct-4K preset completed both nine-frame and **121-frame / 24 fps** jobs in **73.208 seconds** and **1041.038 seconds** respectively. Both S3 outputs loaded at **3840×2160**, lasting **0.375 seconds** and **5.041667 seconds**, on the user's 48 GB worker. **The user subsequently reported color artifacts in the longer clip; successful execution is not a quality qualification.** At that point the tester had **21 presets** and passed **62 tests**. This preset accepts only **9 or 121 frames**, retaining nine as the default; it uses one full-resolution diffusion stage and a final crop. The tester gives the 121-frame job a one-hour Runpod execution timeout and a two-hour TTL; the worker's own timeout still applies. [Direct 4K evidence](native-4k.md)

**2026-09-12 live generation:** the deployed ComfyUI worker completed a 1280×768, 121-frame job, then two real **3840×2160 image-to-video jobs** of 9 and 33 frames at 24 fps. Both 4K jobs reported worker success, returned an S3 video, and loaded in the browser at the expected dimensions and durations. The 33-frame clip took 266.635 seconds of execution and lasts 1.375 seconds. At that point the tester had **20 presets** and passed **61 tests**; the two Comfy upscale presets used the shipped-graph compatibility transport. These jobs did not run the Python DFR pipeline. See [4K verification and remaining limits](4k.md). The historical fixture checks below remain separately identified.

Local verification completed on **2026-09-11**:

- **53 local tester tests passed in 1.668 seconds** with `python -m unittest discover -s tools/runpod_tester -p "test_*.py" -q`, covering the original proxy and the S3 upload extension. The prior proxy-only run passed 27 tests in 0.892 seconds. JavaScript syntax checking with `node --check` passed.
- All **18 tester presets** passed the real worker's `prepare_input()` validation using placeholder media and matching motion-track points where required. This did not decode media or generate outputs.
- The running local server returned endpoint `dfadob3rm5dg32`, all 18 modes, and `api_key_configured: false` through its configuration endpoint.
- Browser checks against a **synthetic local API fixture** passed health display, `IN_QUEUE` → `RUNNING` → `COMPLETED`, the expected missing-image failure, cancellation, and construction of a request combining a first-frame file with an independent last-frame HTTPS URL. A fixture MP4 played successfully at **512×320, nine frames, 0.375 seconds**, with browser readiness state 4 and no media error.
- Fixture checks also confirmed that `COMPLETED` plus a worker error remains a review-needed result with **No generation verified**, and an uncertain submission blocks another Run until explicit Detach.
- S3 browser fixture checks passed a raw **7,340,032-byte PNG** first-frame upload, automatic signed-URL insertion, the Copy URL control, and expiry display. A separate last-frame upload received a different URL while preserving the first frame's URL. A simulated denied GET/readback check retained the fixture object and showed an error without switching that input to URL mode or changing the first frame's URL.

The September 11 fixture video and simulated job responses tested the page and transport behavior; **they were not LTX-generated results**. No external Runpod job was submitted and no model inference was performed during those checks. The live page at [localhost:8766](http://localhost:8766) is configured for the user's endpoint; a Runpod API key is required to check it or submit a real generation.

The September 11 S3 browser checks used a synthetic local fixture, with no live S3 upload or Runpod job at that stage. Local package metadata confirmed that Boto3 1.35.31 and the installed Botocore 1.35.31, s3transfer 0.10.2, and jmespath 1.0.1 satisfy the SDK dependency ranges; [the official tagged package definition](https://github.com/boto/boto3/blob/1.35.31/setup.py) records those requirements. This metadata check did not verify bucket permissions or upload behavior.

**2026-09-12 upload error fix:** a malformed endpoint on a 7 MiB upload was confirmed to trigger Windows connection reset `WinError 10054` before `upload_media()` ran. The fix adds `POST /api/s3/validate` before binary transfer, explicit endpoint/region help, and bounded draining of rejected request bodies so the local server can return its error. **57 combined local tests passed in 1.753 seconds**, including a 7 MiB regression that now returns HTTP 400 instead of the connection reset, preflight validation without storage-network calls, and R2 region checks. JavaScript syntax and whitespace checks passed. Browser checks verified specific errors for an endpoint containing a bucket path, Region `WEUR`, a missing secret, and an invalid prefix rejected by `/api/s3/validate`, all before binary upload. The 53-test result above remains the September 11 baseline.

**Real source upload verified on 2026-09-12:** after correcting the endpoint and region, a **7,605,951-byte PNG** uploaded successfully to Cloudflare R2 using the user's existing page credentials. The signed GET range check confirmed the total object size and matched the first 64 source bytes. The page reported the upload as verified and automatically selected the resulting HTTPS URL for the image input. This verifies **source upload and readback only**; no Runpod generation or LTX inference was performed by this test.
