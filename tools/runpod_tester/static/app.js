"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT", "ERROR"]);
  const COMMON = ["width", "height", "num_frames", "fps", "seed"];
  const MAX_FILE_BYTES = 6 * 1024 * 1024;
  const MAX_INPUT_BYTES = 9000000;
  const S3_FIELDS = ["s3Endpoint", "s3Region", "s3Bucket", "s3AccessKey", "s3SecretKey", "s3SessionToken", "s3Prefix", "s3Expires", "s3Addressing"];
  const LABELS = {num_frames: "Frames", fps: "FPS", first_frame: "First frame", last_frame: "Last frame", mask_video: "Mask video", tracks_json: "Motion tracks JSON"};
  const MODE_LABELS = {image_to_video_4k:"Image to video · Comfy upscale 4K",video_upscale_4k:"Video · Comfy upscale 4K",image_to_video_native_4k:"Image to video · Experimental direct 4K",image_to_video_dfr_4k:"Image to video · Official DFR 4K",text_to_video_dfr_4k:"Text to video · Official DFR 4K",video_enhance_cq_v2:"Video enhance · CQ V2"};
  const DESCRIPTIONS = {
    text_to_video: "A small text-to-video job tests real model generation without a media upload.",
    image_to_video: "Animate an image. Choose a local file, the sample image, or a signed S3 URL.",
    image_to_video_4k: "Generate a 3840 × 2160 video using the ComfyUI two-stage workflow followed by the pixel upscaler. This is distinct from the official Python DFR pipeline.",
    image_to_video_native_4k: "Experimental single-pass 4K. Both 9- and 121-frame jobs completed, but color artifacts were reported in the longer clip. Completion does not establish visual quality; the cause has not been diagnosed.",
    image_to_video_dfr_4k: "Animate an image using the official Python DFR staged 4K pipeline. Requires the separate DFR worker image and its endpoint.",
    text_to_video_dfr_4k: "Generate a video from text using the official Python DFR staged 4K pipeline. Requires the separate DFR worker image and its endpoint.",
    first_last_frame: "Guide the opening and ending frames. Each image can use its own source format.",
    video_to_video: "Guide generation with edges extracted from a source video.",
    video_upscale_x2: "Use the dedicated LTX 2.5 pixel upscaler LoRA. Output width and height are doubled.",
    video_upscale_4k: "Upscale a source video to 3840 × 2160 with the dedicated LTX 2.5 pixel upscaler LoRA.",
    text_to_audio: "Generate an audio clip from text. This mode has no image dimensions.",
    video_deblur: "Deblur a source video with the official IC-LoRA. The source must contain audio.",
    video_inpaint: "Replace white mask areas and retain black areas. Both videos must align; the source needs audio.",
    video_outpaint: "Extend the source canvas. This mode requires source audio and uses two stages.",
    motion_track: "Guide an image with motion trajectories. Supply one position per requested frame in More settings.",
    video_enhance_cq_v2: "Restore detail and clarity with CQ Enhancer V2 at 30 FPS. For Full HD, use 1920 × 1088 and crop four rows at each edge afterward. Experimental 2560 × 1440 requires both CQ pixel caps enabled. Its default limit is 25 frames; longer jobs need an explicit MAX_CQ_HIGH_RES_FRAMES setting and GPU qualification.",
  };
  const state = {config: null, mode: null, fields: new Map(), media: new Map(), validation: false,
    submitting: false, active: null, pollTimer: null, pollBusy: false, pollFailures: 0,
    lastResponse: null, outputSignature: null, uploading: null};

  function node(tag, className, content) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (content !== undefined) element.textContent = content;
    return element;
  }
  function friendly(value) { return LABELS[value] || value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()); }
  function message(id, value, tone = "") {
    const element = $(id);
    element.textContent = value || "";
    element.hidden = !value;
    if (element.classList.contains("notice")) element.className = "notice" + (tone ? " " + tone : "");
  }
  function displayError(error) {
    const raw = error && typeof error.message === "string" ? error.message : "The request could not be completed.";
    return redactString(raw).slice(0, 1600);
  }
  function busyJob() { return Boolean(state.active && !state.active.detached && !TERMINAL.has(state.active.status)); }
  function setControls() {
    const jobLocked = state.submitting || busyJob();
    const locked = jobLocked || Boolean(state.uploading);
    $("runButton").disabled = locked || !state.config;
    $("runButton").textContent = state.submitting ? "Submitting…" : state.uploading ? "Uploading source…" : busyJob() ? "Job in progress" : state.validation ? "Run validation test" : "▶  Run test";
    $("validationButton").disabled = locked || !state.config || !state.config.validation_preset_available;
    $("modeSelect").disabled = locked || !state.config;
    $("endpointId").disabled = jobLocked;
    $("apiKey").disabled = jobLocked;
    for (const id of S3_FIELDS) $(id).disabled = Boolean(state.uploading);
    $("clearS3Credentials").disabled = Boolean(state.uploading);
    for (const record of state.media.values()) {
      const mediaLocked = Boolean(state.uploading) || state.submitting;
      record.file.disabled = mediaLocked;
      record.url.disabled = mediaLocked;
      record.fileButton.disabled = mediaLocked;
      record.urlButton.disabled = mediaLocked;
      if (record.sampleButton) record.sampleButton.disabled = mediaLocked || Boolean(record.sampleLoading);
      record.uploadButton.disabled = mediaLocked || !state.config?.s3_upload?.available || !record.sourceFile && !record.data;
      record.uploadButton.textContent = state.uploading?.record === record ? "Uploading…" : "Upload to S3";
    }
    const active = busyJob();
    $("checkStatusButton").disabled = !active || !state.active.id || state.pollBusy === state.active;
    $("cancelButton").disabled = !active || !state.active.id || Boolean(state.active.cancelRequested);
    $("cancelButton").textContent = state.active && state.active.cancelRequested ? "Cancel requested" : "Cancel job";
    $("detachButton").disabled = !active || state.submitting;
    $("attachButton").disabled = state.submitting || Boolean(active && state.active.id);
  }
  function credentials() {
    const endpoint = $("endpointId").value.trim();
    const key = $("apiKey").value.trim();
    if (!endpoint) throw new Error("Enter your Runpod endpoint ID first.");
    if (!key && !state.config?.api_key_configured) throw new Error("Enter a Runpod API key, or configure one on the local server.");
    return {endpoint_id: endpoint, api_key: key};
  }
  async function api(path, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 65000);
    try {
      const response = await fetch(path, {method: payload === undefined ? "GET" : "POST",
        headers: payload === undefined ? {} : {"Content-Type": "application/json"},
        body: payload === undefined ? undefined : JSON.stringify(payload),
        cache: "no-store", signal: controller.signal});
      let data;
      try { data = await response.json(); }
      catch (_) { throw Object.assign(new Error("The local server returned an unreadable response."), {httpStatus: response.status, uncertain: true}); }
      if (!response.ok) throw Object.assign(new Error(typeof data.error === "string" ? data.error : "The request was rejected."),
        {httpStatus: response.status, uncertain: data.submission_uncertain === true || response.status >= 500});
      return data;
    } catch (error) {
      if (path === "/api/s3/validate" && error.name === "AbortError") throw new Error("S3 settings validation timed out. No file was uploaded.");
      if (path === "/api/s3/validate" && error instanceof TypeError) throw new Error("The local upload server could not be reached while checking settings. No file was uploaded.");
      if (error.name === "AbortError") throw Object.assign(new Error("The local request timed out. The submitted job may still be running."), {uncertain: true});
      if (error instanceof TypeError) throw Object.assign(new Error("The local server could not be reached. Keep the job ID and check again."), {uncertain: true});
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function mediaUploadLimit(kind) {
    return kind === "image" ? state.config?.s3_upload?.max_image_bytes || 20 * 1024 * 1024
      : state.config?.s3_upload?.max_media_bytes || 256 * 1024 * 1024;
  }
  function s3Config() {
    const invalid = (id, text) => { $("s3Settings").open = true; $(id).focus(); throw new Error(text); };
    const endpointValue = $("s3Endpoint").value.trim();
    let endpoint;
    try { endpoint = new URL(endpointValue); }
    catch (_) { invalid("s3Endpoint", "Enter the complete HTTPS S3 service endpoint URL. Put the bucket name in the separate Bucket field."); }
    if (endpoint.protocol !== "https:" || endpoint.username || endpoint.password || endpoint.port && endpoint.port !== "443") {
      invalid("s3Endpoint", "The S3 endpoint must use HTTPS on port 443, without embedded credentials.");
    }
    if (endpoint.pathname !== "/" || endpoint.search || endpoint.hash) {
      invalid("s3Endpoint", "Remove the bucket or other path from the S3 endpoint URL. Use only the HTTPS service hostname, and put the bucket name in the separate Bucket field.");
    }
    const region = $("s3Region").value.trim();
    if (endpoint.hostname.endsWith(".r2.cloudflarestorage.com") && !["auto", "us-east-1"].includes(region)) {
      invalid("s3Region", "For Cloudflare R2, set Region to auto (or us-east-1). WEUR is a location hint, not an S3 signing region.");
    }
    const fields = {region:"s3Region",bucket:"s3Bucket",access_key_id:"s3AccessKey",secret_access_key:"s3SecretKey"};
    const result = {endpoint_url:endpointValue};
    for (const [key, id] of Object.entries(fields)) {
      const value = $(id).value.trim();
      if (!value) invalid(id, id === "s3SecretKey" ? "Enter the S3 secret access key. An access key ID alone cannot sign an upload." : `Enter ${friendly(key).toLowerCase()} in S3 uploads above.`);
      result[key] = value;
    }
    result.session_token = $("s3SessionToken").value.trim();
    result.prefix = $("s3Prefix").value.trim();
    result.expires_in = Number($("s3Expires").value);
    result.addressing_style = $("s3Addressing").value;
    return result;
  }
  function utf8Base64(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  function sampleBlob(data) {
    const parsed = /^data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)$/.exec(data || "");
    if (!parsed) throw new Error("Select a source file or a sample image first.");
    const binary = atob(parsed[2]);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return new Blob([bytes], {type:parsed[1]});
  }
  function sendS3File(blob, filename, config, record) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/s3/upload", true);
      xhr.timeout = 600000;
      xhr.responseType = "json";
      xhr.setRequestHeader("Content-Type", blob.type || "application/octet-stream");
      xhr.setRequestHeader("X-Upload-Name", encodeURIComponent(filename));
      xhr.setRequestHeader("X-S3-Config", utf8Base64(JSON.stringify(config)));
      xhr.upload.onprogress = (event) => { record.uploadStatus.textContent = event.lengthComputable ? `Sending file to local server… ${Math.round(event.loaded / event.total * 100)}%` : "Sending file to local server…"; };
      xhr.upload.onload = () => { record.uploadStatus.textContent = "Uploading to S3 and verifying the signed URL…"; };
      xhr.onload = () => {
        const result = xhr.response;
        if (xhr.status < 200 || xhr.status >= 300) { reject(new Error(typeof result?.error === "string" ? result.error : `S3 upload failed (HTTP ${xhr.status}).`)); return; }
        if (!result || typeof result !== "object" || result.verified !== true || typeof result.url !== "string") { reject(new Error("The upload did not return a verified signed URL. Inspect your bucket before retrying.")); return; }
        try { httpsUrl(result.url); resolve(result); } catch (_) { reject(new Error("The upload returned an invalid HTTPS URL.")); }
      };
      xhr.onerror = () => reject(new Error("The connection to the local upload server failed. Upload status is unknown; no successful S3 upload has been confirmed."));
      xhr.ontimeout = () => reject(new Error("The upload exceeded 10 minutes. It was not retried; inspect your bucket before uploading again."));
      xhr.onabort = () => reject(new Error("The upload was interrupted. It was not retried; an object may already exist in your bucket."));
      xhr.send(blob);
    });
  }
  function expiryLabel(result) {
    let timestamp = result.expires_at;
    if (typeof timestamp === "number" && timestamp < 1000000000000) timestamp *= 1000;
    const date = new Date(timestamp);
    return Number.isNaN(date.getTime()) ? `Expires in ${Math.round(Number(result.expires_in || 86400) / 3600)} hours` : `Expires ${date.toLocaleString()}`;
  }
  async function uploadMedia(record) {
    if (state.uploading || state.submitting) return;
    record.uploaded = null;
    record.uploadDetails.hidden = true;
    record.uploadStatus.hidden = false; record.uploadStatus.className = "upload-progress";
    try {
      if (!state.config?.s3_upload?.available) throw new Error("S3 uploads are unavailable. Install the tester dependencies shown in S3 uploads above, then restart the local server.");
      const config = s3Config();
      state.uploading = {record,config}; setControls();
      if (record.pending) await record.pending;
      if (record.error) throw new Error(record.error);
      const blob = record.sourceFile || sampleBlob(record.data);
      if (blob.size > mediaUploadLimit(record.item.kind)) throw new Error(`This ${record.item.kind} exceeds the S3 upload limit of ${Math.round(mediaUploadLimit(record.item.kind) / 1048576)} MiB.`);
      const filename = record.sourceFile?.name || `${record.item.role}-sample.png`;
      message("s3Message", "Checking S3 settings with the local server before sending any file bytes…");
      record.uploadStatus.textContent = "Checking S3 settings…";
      const validation = await api("/api/s3/validate", {settings:config,filename,size:blob.size});
      if (validation?.valid !== true) throw new Error("S3 settings could not be validated. No file was uploaded.");
      message("s3Message", `Uploading ${friendly(record.item.role).toLowerCase()} to your configured bucket. The URL is checked before it is used.`);
      record.uploadStatus.textContent = "Starting upload…";
      const result = await sendS3File(blob, filename, config, record);
      record.url.value = result.url; record.uploaded = result; switchMedia(record, "url");
      record.uploadStatus.textContent = "Uploaded and URL verified. This input now uses its S3 URL.";
      record.uploadDetails.hidden = false;
      record.uploadDetailsText.textContent = redactString(`${result.bucket || config.bucket}/${result.key || filename} · ${expiryLabel(result)}`);
      record.copyUrl.hidden = false;
      message("s3Message", `${friendly(record.item.role)} uploaded and URL verified. Its signed URL is ready in that media input.`);
    } catch (error) {
      const text = displayError(error); record.uploadStatus.textContent = text; record.uploadStatus.classList.add("error-text");
      message("s3Message", text, "error");
    } finally { state.uploading = null; setControls(); }
  }

  function makeParameter(name, rule, value) {
    const wrapper = node("label", rule.type === "string" ? "wide" : "");
    wrapper.append(node("span", "", friendly(name)));
    const field = node(rule.type === "string" ? "textarea" : "input");
    field.id = "parameter-" + name;
    if (rule.type === "string") {
      field.rows = name === "tracks_json" ? 4 : 2;
      field.placeholder = name === "tracks_json" ? '[[{"x":100,"y":150}, … one position per frame …]]' : "";
      if (rule.max_length) field.maxLength = rule.max_length;
    } else {
      field.type = "number";
      if (rule.minimum !== undefined) field.min = String(rule.minimum);
      if (rule.maximum !== undefined) field.max = String(name === "seed" ? Math.min(rule.maximum, Number.MAX_SAFE_INTEGER) : rule.maximum);
      field.step = String(rule.multiple_of || (rule.type === "integer" ? 1 : "any"));
    }
    const fixed = rule.enum?.length === 1;
    field.value = fixed ? String(rule.enum[0]) : value === undefined ? "" : String(value);
    if (fixed) { field.readOnly = true; field.classList.add("fixed-parameter"); field.setAttribute("aria-readonly", "true"); }
    field.addEventListener("input", updateDimensionHint);
    wrapper.append(field);
    if (fixed) wrapper.append(node("span", "parameter-hint", "Fixed for this mode"));
    else if (rule.multiple_of) wrapper.append(node("span", "parameter-hint", rule.offset ? `${rule.multiple_of}n + ${rule.offset}` : `Multiple of ${rule.multiple_of}`));
    if (name === "seed") wrapper.append(node("span", "parameter-hint", "Exact integers up to 9,007,199,254,740,991"));
    state.fields.set(name, {field, rule});
    return wrapper;
  }
  function switchMedia(record, type) {
    record.type = type;
    record.filePanel.hidden = type !== "file";
    record.urlPanel.hidden = type !== "url";
    record.fileButton.setAttribute("aria-pressed", String(type === "file"));
    record.urlButton.setAttribute("aria-pressed", String(type === "url"));
  }
  function fileData(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("This file could not be read. Choose it again."));
      reader.onabort = () => reject(new Error("The file read was cancelled."));
      reader.readAsDataURL(file);
    });
  }
  function makeMedia(item) {
    const card = node("div", "media-role");
    const heading = node("div", "media-role-heading");
    heading.append(node("span", "media-role-name", friendly(item.role) + (item.required === false ? " · optional" : "")));
    const toggle = node("div", "source-switch");
    toggle.setAttribute("aria-label", `${friendly(item.role)} source`);
    const fileButton = node("button", "", "File"); fileButton.type = "button";
    const urlButton = node("button", "", "HTTPS URL"); urlButton.type = "button";
    toggle.append(fileButton, urlButton); heading.append(toggle);
    const filePanel = node("div");
    const file = node("input"); file.type = "file"; file.accept = `${item.kind}/*`;
    file.setAttribute("aria-label", `${friendly(item.role)} file`);
    const tools = node("div", "media-file-tools");
    const status = node("span", "media-status", "No file selected"); tools.append(status);
    const uploadRow = node("div", "media-upload-row");
    const uploadButton = node("button", "button secondary compact", "Upload to S3"); uploadButton.type = "button";
    uploadButton.setAttribute("aria-label", `Upload ${friendly(item.role).toLowerCase()} to S3`);
    uploadRow.append(uploadButton, node("span", "media-upload-hint", "Get a signed URL using S3 uploads above"));
    filePanel.append(file, tools, uploadRow);
    const urlPanel = node("div");
    const url = node("input"); url.type = "url"; url.placeholder = "https://bucket.s3.amazonaws.com/file?…";
    url.autocomplete = "off"; url.spellcheck = false; url.setAttribute("aria-label", `${friendly(item.role)} HTTPS URL`);
    const urlTools = node("div", "url-copy-row");
    const copyUrl = node("button", "button secondary compact", "Copy URL"); copyUrl.type = "button"; copyUrl.hidden = true;
    copyUrl.setAttribute("aria-label", `Copy ${friendly(item.role).toLowerCase()} URL`);
    urlTools.append(node("span", "", "Signed URLs grant temporary access. Keep them private."), copyUrl);
    urlPanel.append(url, urlTools);
    const uploadStatus = node("div", "upload-progress"); uploadStatus.hidden = true; uploadStatus.setAttribute("role", "status");
    const uploadDetails = node("div", "uploaded-source"); uploadDetails.hidden = true;
    const uploadDetailsText = node("p"); uploadDetails.append(uploadDetailsText);
    const record = {item, type: "file", file, url, filePanel, urlPanel, fileButton, urlButton,
      data:null, pending:null, error:null, sourceFile:null, loadId:0, sampleButton:null,
      uploadButton,uploadStatus,uploadDetails,uploadDetailsText,copyUrl,uploaded:null};
    fileButton.addEventListener("click", () => switchMedia(record, "file"));
    urlButton.addEventListener("click", () => switchMedia(record, "url"));
    uploadButton.addEventListener("click", () => uploadMedia(record));
    url.addEventListener("input", () => { copyUrl.hidden = !url.value.trim(); uploadDetails.hidden = true; record.uploaded = null; });
    copyUrl.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(httpsUrl(url.value.trim())); copyUrl.textContent = "Copied"; setTimeout(() => { copyUrl.textContent = "Copy URL"; }, 1500); }
      catch (_) { url.focus(); url.select(); uploadStatus.hidden = false; uploadStatus.textContent = "The URL is selected. Copy it using your keyboard."; }
    });
    file.addEventListener("change", () => {
      const chosen = file.files[0], loadId = ++record.loadId;
      record.data = null; record.error = null; record.sourceFile = chosen || null; record.pending = null;
      record.uploaded = null; uploadDetails.hidden = true; uploadStatus.hidden = true;
      status.className = "media-status";
      if (!chosen) { status.textContent = "No file selected"; setControls(); return; }
      if (chosen.size > mediaUploadLimit(item.kind)) { record.error = `File exceeds the ${Math.round(mediaUploadLimit(item.kind) / 1048576)} MiB S3 limit for ${item.kind} inputs.`; record.sourceFile = null; status.textContent = record.error; status.classList.add("error-text"); setControls(); return; }
      if (chosen.size > MAX_FILE_BYTES) { status.textContent = `${chosen.name} · ${(chosen.size / 1048576).toFixed(1)} MiB — upload to S3 before running`; setControls(); return; }
      status.textContent = "Reading file…";
      record.pending = fileData(chosen).then((value) => {
        if (record.loadId !== loadId) return;
        record.data = value; status.textContent = `${chosen.name} · ${(chosen.size / 1024).toFixed(0)} KiB`;
      }).catch((error) => { if (record.loadId === loadId) { record.error = displayError(error); status.textContent = record.error; status.classList.add("error-text"); } })
        .finally(() => { if (record.loadId === loadId) record.pending = null; setControls(); });
      setControls();
    });
    if (item.kind === "image") {
      const sample = node("button", "sample-button", "Use sample image"); sample.type = "button";
      record.sampleButton = sample;
      sample.addEventListener("click", async () => {
        const loadId = ++record.loadId;
        record.sampleLoading = true; record.sourceFile = null; record.data = null; record.error = null;
        record.uploaded = null; uploadDetails.hidden = true; uploadStatus.hidden = true; file.value = "";
        status.className = "media-status"; status.textContent = "Loading sample…"; setControls();
        try {
          const data = await api("/api/sample-image");
          if (record.loadId !== loadId) return;
          if (typeof data.base64 !== "string" || !/^[A-Za-z0-9+/=\r\n]+$/.test(data.base64)) throw new Error("Sample image is unavailable.");
          record.data = "data:image/png;base64," + data.base64; record.error = null; file.value = "";
          status.textContent = "Built-in sample image selected"; switchMedia(record, "file");
        } catch (error) { if (record.loadId === loadId) { record.error = displayError(error); status.textContent = record.error; status.classList.add("error-text"); } }
        finally { record.sampleLoading = false; setControls(); }
      });
      tools.append(sample);
    }
    switchMedia(record, "file");
    state.media.set(item.role, record); card.append(heading, filePanel, urlPanel, uploadStatus, uploadDetails);
    return card;
  }
  function selectMode(id, validation = false) {
    state.mode = state.config.modes.find((mode) => mode.id === id);
    if (!state.mode) return;
    const dfr = state.config.runtime === "dfr";
    const cq = state.config.runtime === "cq-v2";
    validation = validation && state.config.validation_preset_available;
    state.validation = validation; $("modeSelect").value = id;
    $("modeDescription").textContent = DESCRIPTIONS[id] || state.mode.description || friendly(id);
    $("validationNotice").hidden = !validation;
    const fourK = state.mode.admission_profile === "4k" || (Object.hasOwn(MODE_LABELS, id) && id !== "video_enhance_cq_v2");
    $("fourKNotice").hidden = !fourK;
    const native4K = id === "image_to_video_native_4k";
    $("fourKNotice").querySelector("strong").textContent = dfr ? "Official DFR · separate worker image required" : native4K ? "Experimental direct 4K · color artifacts reported" : "Comfy upscale · start with 9 frames";
    $("fourKNotice").querySelector("span").textContent = dfr
      ? "The DFR image runs 960 × 544 → 1920 × 1088 → 3840 × 2176, then crops to UHD. The default is 33 frames at 24 fps; 9 and 121 are also accepted. Select the DFR endpoint: this page cannot verify the image deployed there. Hardware fit and output quality require a completed test."
      : native4K
        ? "The 121-frame test completed on the 48 GB worker but the user observed color artifacts. This single-pass preset is experimental and is not the official staged DFR recipe. Peak VRAM was not measured."
        : "The Comfy image-to-video upscaling chain completed 9- and 33-frame tests on a 48 GB Runpod GPU. Standalone video upscaling still needs a GPU test. These presets are not the full DFR pipeline.";
    $("presetBadge").textContent = validation ? "Validation only" : dfr ? "DFR · 4K" : cq ? "CQ V2 · 30 FPS" : fourK ? "4K preset" : "Small generation";
    $("validationButton").hidden = !state.config.validation_preset_available;
    $("validationButton").textContent = validation ? "Use generation preset" : "Validation-only preset";
    $("mediaInputs").replaceChildren(); state.media.clear(); state.fields.clear();
    for (const item of state.mode.media || []) $("mediaInputs").append(makeMedia(item));
    $("mediaSection").hidden = !(state.mode.media || []).length || validation;
    $("commonParameters").replaceChildren(); $("advancedParameters").replaceChildren();
    const defaults = state.mode.parameters?.defaults || {};
    const rules = state.mode.parameters?.rules || {};
    const names = [...COMMON.filter((name) => name in rules), ...Object.keys(rules).filter((name) => !COMMON.includes(name))];
    for (const name of names) {
      if (["prompt", "negative_prompt", "cfg"].includes(name) || rules[name].enum?.length === 1 && !COMMON.includes(name)) continue;
      const common = COMMON.includes(name);
      $(common ? "commonParameters" : "advancedParameters").append(makeParameter(name, rules[name], defaults[name]));
    }
    $("negativePrompt").value = "";
    $("prompt").closest("label").hidden = state.mode.prompt_required === false;
    $("negativePrompt").closest("label").hidden = dfr || state.mode.prompt_required === false;
    $("advancedSection").querySelector("p.hint").hidden = dfr || state.mode.prompt_required === false;
    $("advancedSection").hidden = dfr && !$("advancedParameters").childElementCount;
    $("advancedSection").open = id === "motion_track";
    message("formError", ""); updateDimensionHint(); setControls();
  }
  function fieldNumber(name) { const value = state.fields.get(name)?.field.value; return value === undefined || value === "" ? null : Number(value); }
  function updateDimensionHint() {
    const frames = fieldNumber("num_frames"), fps = fieldNumber("fps");
    $("durationHint").textContent = frames && fps ? `${frames} frames · ${(frames / fps).toFixed(2)} seconds` : "";
    const width = fieldNumber("width"), height = fieldNumber("height");
    if (!width || !height) { $("dimensionHint").textContent = "Audio-only mode. Duration follows the frame count and FPS."; return; }
    const scale = state.mode?.output_scale || 1;
    const paddedWidth = width + (fieldNumber("pad_left") || 0) + (fieldNumber("pad_right") || 0);
    const paddedHeight = height + (fieldNumber("pad_top") || 0) + (fieldNumber("pad_bottom") || 0);
    const finalWidth = state.mode?.output_dimensions?.width || paddedWidth * scale;
    const finalHeight = state.mode?.output_dimensions?.height || paddedHeight * scale;
    const fixedSize = Boolean(state.mode?.output_dimensions);
    if (state.config?.runtime === "dfr") {
      $("dimensionHint").textContent = `960 × 544 base → 1920 × 1088 detail → ${width} × ${height} render → ${finalWidth} × ${finalHeight} output. Fixed spatial stages; temporal upscaling is off.`;
      return;
    }
    $("dimensionHint").textContent = `${width} × ${height} source → ${finalWidth} × ${finalHeight} output. `
      + (fixedSize ? "Source dimensions are fixed for this preset; your input is resized to match. Start with 9 frames." : "Small dimensions help limit the first test's memory use.");
  }
  function validateParameter(name, field, rule) {
    const raw = field.value.trim();
    if (rule.type === "string") {
      if (!raw && name === "tracks_json") throw new Error("Add motion tracks in More settings, or choose another mode.");
      if (rule.max_length && raw.length > rule.max_length) throw new Error(`${friendly(name)} is too long.`);
      if (name === "tracks_json") { try { JSON.parse(raw); } catch (_) { throw new Error("Motion tracks must be valid JSON."); } }
      return raw;
    }
    const value = Number(raw);
    if (!raw || !Number.isFinite(value)) throw new Error(`Enter a valid ${friendly(name).toLowerCase()}.`);
    if (rule.type === "integer" && !Number.isSafeInteger(value)) throw new Error(`${friendly(name)} must be a safely representable integer.`);
    if (rule.minimum !== undefined && value < rule.minimum || rule.maximum !== undefined && value > rule.maximum) throw new Error(`${friendly(name)} must be between ${rule.minimum ?? "its minimum"} and ${rule.maximum ?? "its maximum"}.`);
    if (rule.enum && !rule.enum.includes(value)) throw new Error(`${friendly(name)} must be one of: ${rule.enum.join(", ")}.`);
    if (rule.multiple_of && (value - (rule.offset || 0)) % rule.multiple_of !== 0) throw new Error(`${friendly(name)} must be ${rule.offset ? `${rule.multiple_of}n + ${rule.offset}` : `a multiple of ${rule.multiple_of}`}.`);
    return value;
  }
  function httpsUrl(raw) {
    let url;
    try { url = new URL(raw); } catch (_) { throw new Error("Use a complete HTTPS URL for each URL input."); }
    if (url.protocol !== "https:" || url.username || url.password || url.port && url.port !== "443") throw new Error("Media URLs must use HTTPS without embedded credentials or a custom port.");
    return raw;
  }
  async function collectInput() {
    if (state.uploading) throw new Error("Wait for the S3 upload to finish before running a job.");
    if (state.validation) return {mode: "image_to_video", prompt: "Validation-only test: expected missing image failure", media: {}};
    const prompt = $("prompt").value.trim();
    if (state.mode.prompt_required !== false && !prompt) throw new Error("Add a prompt describing what you want to generate.");
    const parameters = {};
    for (const [name, record] of state.fields) parameters[name] = validateParameter(name, record.field, record.rule);
    const media = {};
    for (const [role, record] of state.media) {
      if (record.type === "file") {
        if (record.pending) await record.pending;
        if (record.error) throw new Error(`${friendly(role)}: ${record.error}`);
        if (record.sourceFile?.size > MAX_FILE_BYTES) throw new Error(`${friendly(role)} exceeds the 6 MiB base64 limit. Click Upload to S3 for that input first, then run the job with its URL.`);
        if (record.data) media[role] = record.data;
        else if (record.item.required !== false) throw new Error(`Choose a ${friendly(role).toLowerCase()} file or switch that input to HTTPS URL.`);
      } else {
        const value = record.url.value.trim();
        if (value) media[role] = {url: httpsUrl(value)};
        else if (record.item.required !== false) throw new Error(`Enter the ${friendly(role).toLowerCase()} HTTPS URL.`);
      }
    }
    const input = {mode: state.mode.id, parameters, media};
    if (prompt || state.mode.prompt_required !== false) input.prompt = prompt;
    const negative = $("negativePrompt").value.trim(); if (negative && state.config.runtime !== "dfr") input.negative_prompt = negative;
    if (new TextEncoder().encode(JSON.stringify(input)).length > MAX_INPUT_BYTES) throw new Error("Combined request exceeds 9 MB. Use HTTPS/S3 URLs for one or more media inputs.");
    return input;
  }

  function redactCredentials(value) {
    let result = value;
    for (const secret of sensitiveValues()) result = result.split(secret).join("[credential hidden]");
    return result;
  }
  function redactString(value) {
    // Mask the complete query before inserting redaction text containing spaces.
    const withoutQueries = value.replace(/https:\/\/[^\s"<>]+/g, (match) => {
      try { const url = new URL(match); return url.origin + url.pathname + (url.search || url.hash ? "?[query hidden]" : ""); } catch (_) { return "[URL hidden]"; }
    });
    return redactCredentials(withoutQueries);
  }
  function redactReportText(value) {
    // Full reports retain usable signed URLs; redact credentials outside URLs.
    let result = "", offset = 0;
    for (const match of value.matchAll(/https:\/\/[^\s"<>]+/g)) {
      result += redactCredentials(value.slice(offset, match.index)) + match[0];
      offset = match.index + match[0].length;
    }
    return result + redactCredentials(value.slice(offset));
  }
  function sensitiveValues() {
    return [$("apiKey").value, state.active?.credentials?.api_key, $("s3AccessKey").value, $("s3SecretKey").value, $("s3SessionToken").value,
      state.uploading?.config?.access_key_id,state.uploading?.config?.secret_access_key,state.uploading?.config?.session_token]
      .filter((secret) => typeof secret === "string" && secret.length >= 5)
      .sort((first, second) => second.length - first.length);
  }
  function safeReport(value, hideMedia = true, key = "", depth = 0) {
    if (/api[_-]?key|authorization|secret|access[_-]?key|token/i.test(key)) return "[credential hidden]";
    if (depth > 24) return "[nested content omitted]";
    if (typeof value === "string") {
      if (!hideMedia) return redactReportText(value);
      if (/^data:[^;]+;base64,/i.test(value) || key === "base64" || value.length > 300 && /^[A-Za-z0-9+/=\r\n]+$/.test(value)) return `[base64 media hidden · ${value.length.toLocaleString()} characters]`;
      const text = redactString(value); return text.length > 6000 ? text.slice(0, 6000) + "… [truncated]" : text;
    }
    if (Array.isArray(value)) return value.map((item) => safeReport(item, hideMedia, key, depth + 1));
    if (value && typeof value === "object") return Object.fromEntries(Object.entries(value).map(([name, item]) => [name,
      hideMedia && value.type === "base64" && name === "data" && typeof item === "string"
        ? `[base64 media hidden · ${item.length.toLocaleString()} characters]`
        : safeReport(item, hideMedia, name, depth + 1)]));
    return value;
  }
  function artifactSource(artifact, kind) {
    if (!artifact || typeof artifact.data !== "string") return null;
    if (artifact.type === "s3_url") { try { return httpsUrl(artifact.data); } catch (_) { return null; } }
    if (artifact.type !== "base64") return null;
    const extension = String(artifact.filename || "").split(".").pop().toLowerCase();
    const types = {mp4:"video/mp4",webm:"video/webm",mov:"video/quicktime",flac:"audio/flac",wav:"audio/wav",mp3:"audio/mpeg",ogg:"audio/ogg",m4a:"audio/mp4",png:"image/png",jpg:"image/jpeg",jpeg:"image/jpeg",webp:"image/webp"};
    const fallback = {videos:"video/mp4",audio:"audio/flac",images:"image/png",files:"application/octet-stream"};
    const mime = types[extension] || fallback[kind];
    if (!mime || kind !== "files" && !mime.startsWith({videos:"video/",audio:"audio/",images:"image/"}[kind])) return null;
    let base64 = artifact.data;
    const dataUri = /^data:([^;,]+);base64,([A-Za-z0-9+/=\r\n]+)$/.exec(base64);
    if (dataUri) { if (dataUri[1] !== mime) return null; base64 = dataUri[2]; }
    if (!/^[A-Za-z0-9+/=\r\n]+$/.test(base64)) return null;
    return `data:${mime};base64,${base64}`;
  }
  function renderOutputs(response) {
    const output = response && typeof response.output === "object" && response.output ? response.output : {};
    const signature = JSON.stringify(output);
    if (signature === state.outputSignature) return;
    state.outputSignature = signature; $("outputMedia").replaceChildren();
    for (const kind of ["videos", "audio", "images", "files"]) {
      const artifacts = Array.isArray(output[kind]) ? output[kind] : [];
      for (const [index, artifact] of artifacts.entries()) {
        const src = artifactSource(artifact, kind); if (!src) continue;
        const card = node("div", "output-card");
        const tag = {videos:"video",audio:"audio",images:"img"}[kind];
        if (tag) {
          const preview = node(tag); preview.src = src; preview.referrerPolicy = "no-referrer";
          if (tag === "img") { preview.alt = "Returned image " + (index + 1); preview.loading = "lazy"; }
          else { preview.controls = true; preview.preload = "metadata"; if (tag === "video") preview.playsInline = true; }
          card.append(preview);
        }
        const filename = typeof artifact.filename === "string" ? artifact.filename : `${kind}-${index + 1}`;
        const info = node("div", "output-info"); info.append(node("span", "", filename));
        const download = node("a", "", "Download ↗"); download.href = src; download.download = filename.replace(/[/\\]/g, "_"); download.target = "_blank"; download.rel = "noopener noreferrer";
        info.append(download); card.append(info); $("outputMedia").append(card);
      }
    }
    if (Array.isArray(output.texts)) for (const item of output.texts) {
      const text = typeof item === "string" ? item : typeof item?.data === "string" ? item.data : JSON.stringify(safeReport(item));
      const card = node("div", "output-card"); card.append(node("pre", "output-text", redactString(text))); $("outputMedia").append(card);
    }
  }
  function clearPoll() { clearTimeout(state.pollTimer); state.pollTimer = null; }
  function schedulePoll(job) {
    if (state.active !== job || !job.id || !busyJob()) return;
    clearPoll();
    const delay = Math.min(30000, 5000 * (2 ** Math.min(state.pollFailures, 3)));
    state.pollTimer = setTimeout(() => poll(job), delay);
  }
  function metric(parent, label, value) { const element = node("span", "metric"); element.append(node("span", "", label), node("strong", "", String(value))); parent.append(element); }
  function renderJob(response = state.lastResponse) {
    const job = state.active; if (!job) return;
    $("emptyResult").hidden = Boolean(response || job.id);
    $("jobDetails").hidden = false;
    $("jobId").textContent = job.id || (job.status === "SUBMITTING" ? "Waiting for the job ID…" : job.status === "FAILED" ? "Request rejected before a job ID was returned" : "No ID received — submission outcome unknown");
    $("copyJobButton").disabled = !job.id;
    const labels = {IN_QUEUE:"Queued",IN_PROGRESS:"Generating",RUNNING:"Generating",COMPLETED:"Completed",FAILED:"Failed",TIMED_OUT:"Timed out",CANCELLED:"Cancelled",ERROR:"Failed",SUBMITTING:"Submitting",SUBMISSION_UNCERTAIN:"Submission not confirmed",CANCELLING:"Cancelling"};
    $("statusLabel").textContent = job.detached ? "Detached from job" : labels[job.status] || friendly(job.status || "Waiting");
    const descriptions = {IN_QUEUE:"Runpod has the job. A new worker may need time to start.",IN_PROGRESS:"The worker is processing the request. Status checks continue automatically.",RUNNING:"The worker is processing the request. Status checks continue automatically.",COMPLETED:"The endpoint returned a completed result. Inspect the output below.",FAILED:"The endpoint reported a failed job. The response below explains why.",TIMED_OUT:"Runpod timed out this job. Inspect the response and worker logs.",CANCELLED:"Runpod marked this job cancelled.",SUBMITTING:"Sending one job to your endpoint. Please wait for its ID.",SUBMISSION_UNCERTAIN:"A job may have been created. Check Runpod, then attach its ID below. The request will not be resubmitted.",CANCELLING:"Cancellation requested. Waiting for Runpod's terminal status."};
    $("statusDescription").textContent = job.detached ? "Automatic checks stopped. A job that was still running continues on Runpod." : descriptions[job.status] || "Waiting for the endpoint's next status update.";
    $("statusDot").className = "status-dot " + (job.detached ? "warning" : job.status === "COMPLETED" ? "success" : ["FAILED","ERROR","TIMED_OUT"].includes(job.status) ? "error" : TERMINAL.has(job.status) ? "warning" : "active");
    $("jobTiming").replaceChildren();
    if (typeof response?.delayTime === "number") metric($("jobTiming"), "Queue", `${(response.delayTime / 1000).toFixed(1)}s`);
    if (typeof response?.executionTime === "number") metric($("jobTiming"), "Execution", `${(response.executionTime / 1000).toFixed(1)}s`);
    if (response) {
      $("jsonSection").hidden = false; $("resultJson").textContent = JSON.stringify(safeReport(response), null, 2);
      renderOutputs(response);
      const output = response.output && typeof response.output === "object" ? response.output : {};
      const expectedKind = {video:"videos",audio:"audio",image:"images"}[state.config?.modes.find((mode) => mode.id === job.mode)?.output_kind];
      const workerError = Boolean(response.error || output.error || (Array.isArray(output.errors) && output.errors.length));
      const validGeneration = !job.validation && Boolean(job.mode) && output.success === true && !workerError
        && Boolean(expectedKind && Array.isArray(output[expectedKind]) && output[expectedKind].some((artifact) => artifactSource(artifact, expectedKind)));
      if (job.status === "COMPLETED" && !validGeneration && !job.detached) {
        $("statusDot").className = "status-dot " + (workerError ? "error" : "warning");
        $("statusLabel").textContent = "Completed · review needed";
        $("statusDescription").textContent = "Runpod marked this job completed; successful generation output has not been verified by this page.";
      }
      const error = typeof response.error === "string" ? response.error : typeof response.output?.error === "string" ? response.output.error : "";
      if (job.validation && job.status === "FAILED" && error.includes("Missing required media input 'image'")) message("resultMessage", "Validation behaved as expected: the missing image was rejected. No generation was tested.");
      else if (error) message("resultMessage", redactString(error), "error");
      else if (job.status === "COMPLETED" && job.validation) message("resultMessage", "This validation preset expected FAILED, but the endpoint returned COMPLETED. Check that it runs the intended worker.", "amber");
      else if (job.status === "COMPLETED" && validGeneration) message("resultMessage", "The worker reported success and returned the expected media. Play the output to assess generation and quality.");
      else if (job.status === "COMPLETED" && !job.mode) message("resultMessage", "This attached job returned COMPLETED. Inspect its response and output; this page did not submit or verify its generation mode.", "amber");
      else if (job.status === "COMPLETED") message("resultMessage", "The endpoint returned COMPLETED, but a successful response with the expected media is missing or invalid. Inspect the response and worker logs.", "amber");
      else message("resultMessage", "");
      if (job.status === "COMPLETED" && validGeneration) { $("verificationBadge").textContent = "Generation result returned"; $("verificationBadge").className = "pill"; }
    }
    setControls();
  }
  function applyResponse(job, response) {
    if (state.active !== job || job.detached || TERMINAL.has(job.status)) return;
    if (response && typeof response === "object" && !Array.isArray(response)) {
      if (!job.id && typeof response.id === "string" && response.id) job.id = response.id;
      if (typeof response.status === "string" && response.status) job.status = response.status.toUpperCase();
      state.lastResponse = response;
    }
    if (TERMINAL.has(job.status)) { job.finished = Date.now(); clearPoll(); message("pollMessage", ""); }
    renderJob(response);
  }
  async function poll(job = state.active) {
    if (!job || state.active !== job || !job.id || !busyJob() || state.pollBusy === job) return;
    clearPoll(); state.pollBusy = job; setControls();
    try {
      const response = await api("/api/status", {...job.credentials, job_id: job.id});
      if (state.active === job && !job.detached) { state.pollFailures = 0; message("pollMessage", ""); applyResponse(job, response); }
    }
    catch (error) {
      if (state.active === job && !job.detached) { state.pollFailures += 1; const wait = Math.min(30, 5 * (2 ** Math.min(state.pollFailures, 3))); message("pollMessage", `${displayError(error)} Keeping this job ID and checking again in ${wait} seconds. You can also use Check status.`, "amber"); }
    } finally { if (state.pollBusy === job) state.pollBusy = false; setControls(); schedulePoll(job); }
  }
  async function submit(event) {
    event.preventDefault(); if (state.submitting || busyJob() || state.uploading) return;
    message("formError", "");
    let auth, input;
    state.submitting = true; setControls();
    try { auth = credentials(); input = await collectInput(); }
    catch (error) { message("formError", displayError(error), "error"); state.submitting = false; setControls(); return; }
    clearPoll(); state.pollFailures = 0; state.lastResponse = null; state.outputSignature = null;
    $("verificationBadge").textContent = "No generation verified"; $("verificationBadge").className = "pill neutral";
    $("outputMedia").replaceChildren(); $("jsonSection").hidden = true; message("resultMessage", ""); message("pollMessage", "");
    const job = {id:null,status:"SUBMITTING",credentials:auth,started:Date.now(),validation:state.validation,mode:input.mode};
    state.active = job; renderJob();
    try {
      const response = await api("/api/run", {...auth, input});
      applyResponse(job, response);
      if (!job.id && !TERMINAL.has(job.status)) { job.status = "SUBMISSION_UNCERTAIN"; message("pollMessage", "No job ID was returned. Check the Runpod dashboard before considering another submission. Attach an existing job ID to resume polling.", "amber"); renderJob(response); }
      else { if (job.status === "SUBMITTING") job.status = "IN_QUEUE"; renderJob(response); schedulePoll(job); }
    } catch (error) {
      if (error.uncertain || !error.httpStatus) { job.status = "SUBMISSION_UNCERTAIN"; message("pollMessage", displayError(error) + " Do not submit again until you check Runpod for an existing job.", "amber"); }
      else { job.status = "FAILED"; job.finished = Date.now(); message("formError", displayError(error), "error"); }
      renderJob();
    } finally { state.submitting = false; setControls(); }
  }
  async function health() {
    $("healthButton").disabled = true; $("healthButton").textContent = "Checking…";
    $("healthMetrics").replaceChildren(); $("healthMetrics").hidden = true;
    try {
      const response = await api("/api/health", credentials());
      $("healthMessage").textContent = "Endpoint reached. Worker counts show availability only; submit a generation job to test LTX.";
      $("healthMessage").className = "inline-note is-success";
      const workers = response?.workers;
      if (workers && typeof workers === "object") {
        for (const [key, value] of Object.entries(workers)) if (typeof value === "number") metric($("healthMetrics"), friendly(key), value);
        $("healthMetrics").hidden = !$("healthMetrics").childElementCount;
        if (Object.values(workers).filter((value) => typeof value === "number").every((value) => value === 0)) $("healthMessage").textContent = "Endpoint reached. No workers are currently listed; a submitted job may trigger a cold start. This is not a model-test failure.";
      }
    } catch (error) { $("healthMessage").textContent = displayError(error); $("healthMessage").className = "inline-note is-error"; }
    finally { $("healthButton").disabled = false; $("healthButton").textContent = "Check endpoint"; }
  }
  async function cancel() {
    const job = state.active; if (!job?.id || !busyJob() || job.cancelRequested) return;
    job.cancelRequested = true; setControls();
    try { const response = await api("/api/cancel", {...job.credentials,job_id:job.id}); applyResponse(job, response); if (state.active === job && !job.detached && !TERMINAL.has(job.status)) message("pollMessage", "Cancellation requested. Checking Runpod until a terminal status is returned.", "amber"); }
    catch (error) { job.cancelRequested = false; if (state.active === job && !job.detached) message("pollMessage", displayError(error) + " The job remains attached; its status will continue to be checked.", "amber"); }
    finally { setControls(); schedulePoll(job); }
  }
  function detach() {
    if (!busyJob() || state.submitting) return;
    const job = state.active; job.detached = true; job.finished = Date.now(); clearPoll();
    message("pollMessage", `Detached${job.id ? " from " + job.id : " from the uncertain submission"}. This does not cancel a job. Check Runpod before submitting again if duplicate work is possible.`, "amber"); renderJob();
  }
  async function attach() {
    if (state.submitting || busyJob() && state.active.id) return;
    try {
      const auth = credentials(), id = $("attachJobId").value.trim();
      if (!id || id.length > 200 || /\s/.test(id)) throw new Error("Enter a valid existing job ID.");
      clearPoll(); state.lastResponse = null; state.outputSignature = null; state.pollFailures = 0;
      $("verificationBadge").textContent = "No generation verified"; $("verificationBadge").className = "pill neutral";
      state.active = {id,status:"IN_QUEUE",credentials:auth,started:Date.now(),validation:false,mode:null};
      message("pollMessage", ""); message("resultMessage", ""); $("outputMedia").replaceChildren(); $("jsonSection").hidden = true;
      renderJob(); await poll(state.active);
    } catch (error) { message("pollMessage", displayError(error), "amber"); }
  }
  function downloadReport() {
    if (!state.lastResponse) return;
    const blob = new Blob([JSON.stringify(safeReport(state.lastResponse, false), null, 2) + "\n"], {type:"application/json"});
    const url = URL.createObjectURL(blob), link = node("a"); link.href = url;
    link.download = `runpod-${(state.active?.id || "response").replace(/[^A-Za-z0-9_-]/g,"_")}.json`;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function initialize() {
    try {
      const config = await api("/api/config");
      if (!Array.isArray(config.modes) || !config.modes.length) throw new Error("No workflow modes were returned by the local server.");
      state.config = config; $("endpointId").value = config.endpoint_id || "";
      if (config.runtime === "dfr") {
        document.querySelector(".page-header h1").textContent = "Try your official DFR worker";
        document.querySelector(".page-header .brand p").textContent = config.runtime_description;
        $("composeHeading").nextElementSibling.textContent = "The DFR preset starts at 33 frames; choose 9 for a shorter test.";
        $("statusDescription").textContent = "Connect the separate DFR endpoint, then submit one staged 4K test.";
        $("endpointId").placeholder = "Your DFR worker endpoint ID";
      } else if (config.runtime === "cq-v2") {
        document.querySelector(".page-header h1").textContent = "Try your CQ Enhancer V2 worker";
        document.querySelector(".page-header .brand p").textContent = config.runtime_description;
        $("composeHeading").nextElementSibling.textContent = "Start with 33 frames. Larger resolutions need shorter sections.";
        $("statusDescription").textContent = "Connect the separate cq-v2 endpoint, enhance one source video, and inspect the result.";
        $("endpointId").placeholder = "Your CQ V2 worker endpoint ID";
      }
      const s3Available = config.s3_upload?.available === true;
      $("s3Availability").textContent = s3Available ? "Available" : "Setup needed";
      $("s3Availability").className = "pill small" + (s3Available ? "" : " neutral");
      message("s3Unavailable", s3Available ? "" : "S3 uploads need the tester dependencies. Run: python -m pip install -r tools/runpod_tester/requirements.txt — then restart the local server and reload this page.", "amber");
      const defaultExpiry = String(config.s3_upload?.default_expires_in || 86400);
      if ([...$("s3Expires").options].some((option) => option.value === defaultExpiry)) $("s3Expires").value = defaultExpiry;
      if (config.api_key_configured) { $("apiKey").placeholder = "Using key configured on local server"; $("keyHint").textContent = "Server key is available. Enter a key only to override it for this page."; }
      $("modeSelect").replaceChildren();
      const priority = ["video_enhance_cq_v2","image_to_video_dfr_4k","text_to_video_dfr_4k","text_to_video","image_to_video","image_to_video_4k","image_to_video_native_4k","first_last_frame","video_to_video","video_upscale_x2","video_upscale_4k","text_to_audio"];
      const modes = [...config.modes].sort((a,b) => (priority.includes(a.id) ? priority.indexOf(a.id) : 99) - (priority.includes(b.id) ? priority.indexOf(b.id) : 99));
      for (const mode of modes) { const option = node("option", "", MODE_LABELS[mode.id] || mode.label || friendly(mode.id)); option.value = mode.id; $("modeSelect").append(option); }
      selectMode(modes.find((mode) => mode.id === "text_to_video")?.id || modes[0].id);
    } catch (error) { message("formError", displayError(error) + " Restart or check the local tester server, then reload this page.", "error"); }
    setControls();
  }

  $("jobForm").addEventListener("submit", submit);
  $("healthButton").addEventListener("click", health);
  $("clearS3Credentials").addEventListener("click", () => {
    if (state.uploading) return;
    for (const id of ["s3AccessKey", "s3SecretKey", "s3SessionToken"]) $(id).value = "";
    message("s3Message", "S3 credentials cleared from the settings. Existing signed URLs keep working until they expire.");
  });
  $("modeSelect").addEventListener("change", () => selectMode($("modeSelect").value));
  $("validationButton").addEventListener("click", () => selectMode(state.validation ? "text_to_video" : "image_to_video", !state.validation));
  $("toggleKey").addEventListener("click", () => { const show = $("apiKey").type === "password"; $("apiKey").type = show ? "text" : "password"; $("toggleKey").textContent = show ? "Hide" : "Show"; $("toggleKey").setAttribute("aria-label", show ? "Hide API key" : "Show API key"); $("toggleKey").setAttribute("aria-pressed", String(show)); });
  $("checkStatusButton").addEventListener("click", () => poll());
  $("cancelButton").addEventListener("click", cancel);
  $("detachButton").addEventListener("click", detach);
  $("attachButton").addEventListener("click", attach);
  $("downloadReportButton").addEventListener("click", downloadReport);
  $("copyJobButton").addEventListener("click", async () => { if (!state.active?.id) return; try { await navigator.clipboard.writeText(state.active.id); $("copyJobButton").textContent = "Copied"; setTimeout(() => { $("copyJobButton").textContent = "Copy"; }, 1500); } catch (_) { message("pollMessage", "Copy is unavailable in this browser. Select and copy the job ID above.", "amber"); } });
  setInterval(() => { const job = state.active; $("elapsed").textContent = job ? `${Math.max(0, Math.floor(((job.finished || Date.now()) - job.started) / 1000))}s elapsed` : ""; }, 1000);
  window.addEventListener("beforeunload", (event) => { if (busyJob() || state.submitting || state.uploading) { event.preventDefault(); event.returnValue = ""; } });
  initialize();
})();
