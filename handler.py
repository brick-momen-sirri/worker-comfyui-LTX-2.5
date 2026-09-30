import runpod
from runpod.serverless.utils import rp_upload
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
import copy
from io import BytesIO
import mimetypes
import re
import websocket
import uuid
import tempfile
import socket
import sqlite3
import traceback
import logging

from credit_estimator import build_empty_credit_usage, estimate_credit_usage
from network_volume import (
    is_network_volume_debug_enabled,
    run_network_volume_diagnostics,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Time to wait between API check attempts in milliseconds
COMFY_API_AVAILABLE_INTERVAL_MS = int(
    os.environ.get("COMFY_API_AVAILABLE_INTERVAL_MS", 50)
)
# Maximum number of API check attempts (0 = no limit, poll while ComfyUI process is alive)
COMFY_API_AVAILABLE_MAX_RETRIES = int(
    os.environ.get("COMFY_API_AVAILABLE_MAX_RETRIES", 0)
)
# Fallback retry limit when PID file is unavailable and retries=0
COMFY_API_FALLBACK_MAX_RETRIES = 500
# PID file written by start.sh so we can detect if ComfyUI has crashed
COMFY_PID_FILE = "/tmp/comfyui.pid"
# Websocket reconnection behaviour (can be overridden through environment variables)
# NOTE: more attempts and diagnostics improve debuggability whenever ComfyUI crashes mid-job.
#   • WEBSOCKET_RECONNECT_ATTEMPTS sets how many times we will try to reconnect.
#   • WEBSOCKET_RECONNECT_DELAY_S sets the sleep in seconds between attempts.
#
# If the respective env-vars are not supplied we fall back to sensible defaults ("5" and "3").
WEBSOCKET_RECONNECT_ATTEMPTS = int(os.environ.get("WEBSOCKET_RECONNECT_ATTEMPTS", 5))
WEBSOCKET_RECONNECT_DELAY_S = int(os.environ.get("WEBSOCKET_RECONNECT_DELAY_S", 3))

# Extra verbose websocket trace logs (set WEBSOCKET_TRACE=true to enable)
if os.environ.get("WEBSOCKET_TRACE", "false").lower() == "true":
    # This prints low-level frame information to stdout which is invaluable for diagnosing
    # protocol errors but can be noisy in production – therefore gated behind an env-var.
    websocket.enableTrace(True)

# Host where ComfyUI is running
COMFY_HOST = "127.0.0.1:8188"
# Enforce a clean state after each job is done
# see https://docs.runpod.io/docs/handler-additional-controls#refresh-worker
REFRESH_WORKER = os.environ.get("REFRESH_WORKER", "false").lower() == "true"

INPUT_DOWNLOAD_TIMEOUT_S = int(os.environ.get("INPUT_DOWNLOAD_TIMEOUT_S", 300))
WORKFLOW_EXECUTION_TIMEOUT_S = max(
    0, int(os.environ.get("WORKFLOW_EXECUTION_TIMEOUT_S", 1200))
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v", ".gif"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}
TEXT_EXTENSIONS = {".txt", ".md", ".json", ".csv", ".log"}
MEDIA_OUTPUT_KEYS = {
    "images": "images",
    "gifs": "videos",
    "videos": "videos",
    "animated": "videos",
    "audio": "audio",
    "files": "files",
}
COMFY_OUTPUT_DIR = os.environ.get("COMFY_OUTPUT_DIR", "/comfyui/output")
MAX_INLINE_OUTPUT_BYTES = max(
    0, int(os.environ.get("MAX_INLINE_OUTPUT_BYTES", 5 * 1024 * 1024))
)
TEXT_ARTIFACT_MAX_INLINE_BYTES = int(
    os.environ.get("TEXT_ARTIFACT_MAX_INLINE_BYTES", 1_000_000)
)
TEXT_ARTIFACT_SCAN_MAX_FILES = int(
    os.environ.get("TEXT_ARTIFACT_SCAN_MAX_FILES", 100)
)
TEXT_ARTIFACT_SCAN_MTIME_GRACE_S = float(
    os.environ.get("TEXT_ARTIFACT_SCAN_MTIME_GRACE_S", 5)
)
CREDIT_KEYWORDS = (
    "credit",
    "credits",
    "cost",
    "usage",
    "billing",
    "charged",
    "spent",
)
CREDIT_TRACKER_DB_PATH = os.environ.get(
    "CREDIT_TRACKER_DB_PATH",
    "/comfyui/custom_nodes/comfyui_credit_tracker/usage_log.db",
)
CREDITS_PER_USD = 211.0

# ---------------------------------------------------------------------------
# Helper: quick reachability probe of ComfyUI HTTP endpoint (port 8188)
# ---------------------------------------------------------------------------


def _comfy_server_status():
    """Return a dictionary with basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


class WorkflowExecutionTimeoutError(TimeoutError):
    """Raised after the worker interrupts a workflow that exceeded its deadline."""


def request_comfyui_interrupt(prompt_id):
    """Best-effort interruption of ComfyUI's currently executing prompt."""
    print(
        f"worker-comfyui - Requesting ComfyUI interrupt for timed-out prompt {prompt_id}..."
    )
    try:
        response = requests.post(f"http://{COMFY_HOST}/interrupt", timeout=10)
        response.raise_for_status()
        print(
            f"worker-comfyui - ComfyUI interrupt requested for prompt {prompt_id}."
        )
        return None
    except requests.RequestException as exc:
        error = f"Failed to interrupt ComfyUI for prompt {prompt_id}: {exc}"
        print(f"worker-comfyui - {error}")
        return error


def raise_if_workflow_timed_out(
    prompt_id, wait_started_at, timeout_s=None, now=None
):
    """Interrupt and raise when a queued workflow exceeds its execution deadline."""
    if timeout_s is None:
        timeout_s = WORKFLOW_EXECUTION_TIMEOUT_S
    if timeout_s <= 0:
        return

    current_time = time.monotonic() if now is None else now
    if current_time - wait_started_at < timeout_s:
        return

    interrupt_error = request_comfyui_interrupt(prompt_id)
    message = (
        f"Workflow execution timed out after {timeout_s:g} seconds for prompt "
        f"{prompt_id}. ComfyUI interrupt requested."
    )
    if interrupt_error:
        message = f"{message} {interrupt_error}"
    raise WorkflowExecutionTimeoutError(message)


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Args:
        ws_url (str): The WebSocket URL (including client_id).
        max_attempts (int): Maximum number of reconnection attempts.
        delay_s (int): Delay in seconds between attempts.
        initial_error (Exception): The error that triggered the reconnect attempt.

    Returns:
        websocket.WebSocket: The newly connected WebSocket object.

    Raises:
        websocket.WebSocketConnectionClosedException: If reconnection fails after all attempts.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        # Log current server status before each reconnect attempt so that we can
        # see whether ComfyUI is still alive (HTTP port 8188 responding) even if
        # the websocket dropped. This is extremely useful to differentiate
        # between a network glitch and an outright ComfyUI crash/OOM-kill.
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            # If ComfyUI itself is down there is no point in retrying the websocket –
            # bail out immediately so the caller gets a clear "ComfyUI crashed" error.
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        # Otherwise we proceed with reconnect attempts while server is up
        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            # Need to create a new socket object for reconnect
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)  # Use existing ws_url
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws  # Return the new connected socket
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    # If loop completes without returning, raise an exception
    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def _validate_named_media_list(job_input, field_name, accepted_data_keys):
    media_items = job_input.get(field_name)
    if media_items is None:
        return None, None

    if not isinstance(media_items, list):
        return None, f"'{field_name}' must be a list"

    for item in media_items:
        if not isinstance(item, dict) or "name" not in item:
            return (
                None,
                f"'{field_name}' must be a list of objects with a 'name' key",
            )

        if not any(key in item for key in accepted_data_keys):
            accepted = "', '".join(accepted_data_keys)
            return (
                None,
                f"Each '{field_name}' item must include one of: '{accepted}'",
            )

    return media_items, None


def validate_input(job_input):
    """
    Validates the input for the handler function.

    Args:
        job_input (dict): The input data to validate.

    Returns:
        tuple: A tuple containing the validated data and an error message, if any.
               The structure is (validated_data, error_message).
    """
    # Validate if job_input is provided
    if job_input is None:
        return None, "Please provide input"

    # Check if input is a string and try to parse it as JSON
    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    if not isinstance(job_input, dict):
        return None, "Input must be a JSON object"

    # Validate 'workflow' in input
    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    # Validate input media, if provided. Images keep the original contract, while
    # videos/files support base64 data or HTTP(S) URLs such as presigned S3 links.
    images, media_error = _validate_named_media_list(
        job_input, "images", ("image", "data", "url")
    )
    if media_error:
        return None, media_error

    videos, media_error = _validate_named_media_list(
        job_input, "videos", ("video", "data", "url")
    )
    if media_error:
        return None, media_error

    files, media_error = _validate_named_media_list(
        job_input, "files", ("file", "data", "url")
    )
    if media_error:
        return None, media_error

    prompt = job_input.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return None, "'prompt' must be a string"

    negative_prompt = job_input.get("negative_prompt")
    if negative_prompt is not None and not isinstance(negative_prompt, str):
        return None, "'negative_prompt' must be a string"

    prompt_replacements = job_input.get("prompt_replacements")
    if prompt_replacements is not None and not isinstance(prompt_replacements, dict):
        return None, "'prompt_replacements' must be an object"

    # Optional: API key for Comfy.org API Nodes, passed per-request
    comfy_org_api_key = job_input.get("comfy_org_api_key") or job_input.get(
        "api_key_comfy_org"
    )

    # Return validated data and no error
    return {
        "workflow": workflow,
        "images": images,
        "videos": videos,
        "files": files,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "prompt_node_id": job_input.get("prompt_node_id"),
        "negative_prompt_node_id": job_input.get("negative_prompt_node_id"),
        "prompt_replacements": prompt_replacements,
        "comfy_org_api_key": comfy_org_api_key,
    }, None


def _get_comfyui_pid():
    """Read the ComfyUI process PID from the PID file written by start.sh."""
    try:
        with open(COMFY_PID_FILE, "r") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_comfyui_process_alive():
    """Check whether the ComfyUI process is still running.

    Returns True if alive, False if dead, None if PID file not found.
    """
    pid = _get_comfyui_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists but we can't signal it


def _looks_like_url(value):
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _decode_data_value(value, filename):
    if _looks_like_url(value):
        response = requests.get(value, timeout=INPUT_DOWNLOAD_TIMEOUT_S)
        response.raise_for_status()
        return response.content

    if not isinstance(value, str):
        raise ValueError(f"Input media for {filename} must be a base64 string or URL")

    if "," in value:
        value = value.split(",", 1)[1]

    return base64.b64decode(value)


def _get_media_payload_value(media_item, data_keys):
    for key in data_keys:
        if key in media_item:
            return media_item[key]
    return None


def _guess_mime_type(filename):
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def _apply_text_to_node(workflow, node_id, text):
    node = workflow.get(str(node_id))
    if not isinstance(node, dict):
        return False
    inputs = node.setdefault("inputs", {})
    if not isinstance(inputs, dict):
        return False
    inputs["text"] = text
    return True


def _find_clip_text_node(workflow, prefer_negative=False):
    candidates = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "CLIPTextEncode":
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict) or "text" not in inputs:
            continue

        title = str(node.get("_meta", {}).get("title", "")).lower()
        score = 0
        if prefer_negative:
            if "negative" in title:
                score += 10
        else:
            if "positive" in title:
                score += 10
            if "negative" in title:
                score -= 5
        if "prompt" in title or "text encode" in title:
            score += 1
        candidates.append((score, str(node_id)))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


def apply_prompt_overrides(
    workflow,
    prompt=None,
    negative_prompt=None,
    prompt_node_id=None,
    negative_prompt_node_id=None,
    prompt_replacements=None,
):
    """
    Apply serverless prompt fields to a ComfyUI API workflow.

    The workflow is copied before mutation so callers can safely reuse their
    original workflow object across jobs.
    """
    if not any([prompt, negative_prompt, prompt_replacements]):
        return workflow

    workflow = copy.deepcopy(workflow)

    if prompt_replacements:
        for target, value in prompt_replacements.items():
            if "." in str(target):
                node_id, input_name = str(target).split(".", 1)
            else:
                node_id, input_name = str(target), "text"

            node = workflow.get(str(node_id))
            if not isinstance(node, dict):
                raise ValueError(f"prompt_replacements target node '{node_id}' not found")
            inputs = node.setdefault("inputs", {})
            if not isinstance(inputs, dict):
                raise ValueError(
                    f"prompt_replacements target node '{node_id}' has no inputs object"
                )
            inputs[input_name] = value

    if prompt is not None:
        target_node_id = prompt_node_id or _find_clip_text_node(
            workflow, prefer_negative=False
        )
        if not target_node_id or not _apply_text_to_node(workflow, target_node_id, prompt):
            raise ValueError(
                "Unable to apply 'prompt'. Provide input.prompt_node_id or "
                "input.prompt_replacements for this workflow."
            )

    if negative_prompt is not None:
        target_node_id = negative_prompt_node_id or _find_clip_text_node(
            workflow, prefer_negative=True
        )
        if not target_node_id or not _apply_text_to_node(
            workflow, target_node_id, negative_prompt
        ):
            raise ValueError(
                "Unable to apply 'negative_prompt'. Provide "
                "input.negative_prompt_node_id or input.prompt_replacements."
            )

    return workflow


def check_server(url, retries=0, delay=50):
    """
    Check if a server is reachable via HTTP GET request.

    When a PID file is available (written by start.sh), the function polls
    indefinitely while the ComfyUI process is alive and fails immediately
    when the process exits.  When no PID file is found it falls back to
    the retry limit for backward compatibility.

    Args:
        url (str): The URL to check.
        retries (int): Max attempts. 0 means unlimited (poll while process alive).
        delay (int): Time in milliseconds between retries.

    Returns:
        bool: True if the server is reachable, False otherwise.
    """
    print(f"worker-comfyui - Checking API server at {url}...")

    # Guard against zero/negative delay to avoid division by zero
    delay = max(1, delay)
    # How often to print a "still waiting" log (every ~10 seconds)
    log_every = max(1, int(10_000 / delay))
    attempt = 0

    while True:
        # --- Check if ComfyUI process is still alive ---
        process_status = _is_comfyui_process_alive()
        if process_status is False:
            print(
                "worker-comfyui - ComfyUI process has exited. "
                "Server will not become reachable."
            )
            return False

        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"worker-comfyui - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException:
            pass

        attempt += 1

        # If we can't track the process, enforce a retry limit to avoid
        # hanging forever when the PID file is never written
        fallback = retries if retries > 0 else COMFY_API_FALLBACK_MAX_RETRIES
        if process_status is None and attempt >= fallback:
            print(
                f"worker-comfyui - Failed to connect to server at {url} "
                f"after {fallback} attempts (no PID file found)."
            )
            return False

        if attempt % log_every == 0:
            elapsed_s = (attempt * delay) / 1000
            print(
                f"worker-comfyui - Still waiting for API server... "
                f"({elapsed_s:.0f}s elapsed, attempt {attempt})"
            )

        time.sleep(delay / 1000)


def upload_input_media(media_items, label, data_keys):
    """
    Upload input media to the ComfyUI input directory using the /upload/image endpoint.

    Args:
        media_items (list): Input media dictionaries with a filename and data/URL value.
        label (str): Human-readable label used in logs.
        data_keys (tuple): Ordered data field names to inspect on each media item.

    Returns:
        dict: A dictionary indicating success or error.
    """
    if not media_items:
        return {"status": "success", "message": f"No {label} to upload", "details": []}

    responses = []
    upload_errors = []

    print(f"worker-comfyui - Uploading {len(media_items)} {label} item(s)...")

    for media_item in media_items:
        try:
            name = media_item["name"]
            payload_value = _get_media_payload_value(media_item, data_keys)
            blob = _decode_data_value(payload_value, name)
            mime_type = media_item.get("mime_type") or _guess_mime_type(name)

            # Prepare the form data
            files = {
                # ComfyUI names this multipart field "image", but it stores the
                # uploaded file by name in the input directory. Video nodes can
                # then reference an uploaded .mp4/.webm the same way image nodes
                # reference an uploaded .png.
                "image": (name, BytesIO(blob), mime_type),
                "overwrite": (None, "true"),
            }

            # POST request to upload the image
            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()

            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = (
                f"Error decoding base64 for {media_item.get('name', 'unknown')}: {e}"
            )
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {media_item.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {media_item.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except ValueError as e:
            error_msg = str(e)
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = (
                f"Unexpected error uploading {media_item.get('name', 'unknown')}: {e}"
            )
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print(f"worker-comfyui - {label} upload finished with errors")
        return {
            "status": "error",
            "message": f"Some {label} failed to upload",
            "details": upload_errors,
        }

    print(f"worker-comfyui - {label} upload complete")
    return {
        "status": "success",
        "message": f"All {label} uploaded successfully",
        "details": responses,
    }


def upload_images(images):
    return upload_input_media(images, "image(s)", ("image", "data", "url"))


def upload_videos(videos):
    return upload_input_media(videos, "video(s)", ("video", "data", "url"))


def upload_files(files):
    return upload_input_media(files, "file(s)", ("file", "data", "url"))


def get_available_models():
    """
    Get list of available models from ComfyUI

    Returns:
        dict: Dictionary containing available models by type
    """
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()

        # Extract available checkpoints from CheckpointLoaderSimple
        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow, client_id, comfy_org_api_key=None):
    """
    Queue a workflow to be processed by ComfyUI

    Args:
        workflow (dict): A dictionary containing the workflow to be processed
        client_id (str): The client ID for the websocket connection
        comfy_org_api_key (str, optional): Comfy.org API key for API Nodes

    Returns:
        dict: The JSON response from ComfyUI after processing the workflow

    Raises:
        ValueError: If the workflow validation fails with detailed error information
    """
    # Include client_id in the prompt payload
    payload = {"prompt": workflow, "client_id": client_id}

    # Optionally inject Comfy.org API key for API Nodes.
    # Precedence: per-request key (argument) overrides environment variable.
    # Note: We use our consistent naming (comfy_org_api_key) but transform to
    # ComfyUI's expected format (api_key_comfy_org) when sending.
    key_from_env = os.environ.get("COMFY_ORG_API_KEY")
    effective_key = comfy_org_api_key if comfy_org_api_key else key_from_env
    if effective_key:
        payload["extra_data"] = {"api_key_comfy_org": effective_key}
    data = json.dumps(payload).encode("utf-8")

    # Use requests for consistency and timeout
    headers = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    # Handle validation errors with detailed information
    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            # Try to extract meaningful error information
            error_message = "Workflow validation failed"
            error_details = []

            # ComfyUI seems to return different error formats, let's handle them all
            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            # Check for node validation errors in the response
            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            # Check if the error data itself contains validation info
            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                # For this type of error, we need to parse the validation details from logs
                # Since ComfyUI doesn't seem to include detailed validation errors in the response
                # Let's provide a more helpful generic message
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            # If we have specific validation errors, format them nicely
            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                # Try to provide helpful suggestions for common errors
                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                # Fallback to the raw response if we can't parse specific errors
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError) as e:
            # If we can't parse the error response, fall back to the raw text
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    # For other HTTP errors, raise them normally
    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    """
    Retrieve the history of a given prompt using its ID

    Args:
        prompt_id (str): The ID of the prompt whose history is to be retrieved

    Returns:
        dict: The history of the prompt, containing all the processing steps and results
    """
    # Use requests for consistency and timeout
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def _execution_terminal_state(message, prompt_id):
    """Return a normalized terminal state for a ComfyUI execution message."""
    if not isinstance(message, dict):
        return None

    message_type = message.get("type")
    data = message.get("data", {})
    if not isinstance(data, dict) or data.get("prompt_id") != prompt_id:
        return None

    if message_type == "execution_success" or (
        message_type == "executing" and data.get("node") is None
    ):
        return {"status": "success", "event": message_type, "data": data}

    if message_type == "execution_error":
        details = (
            f"Node Type: {data.get('node_type')}, "
            f"Node ID: {data.get('node_id')}, "
            f"Message: {data.get('exception_message')}"
        )
        return {
            "status": "error",
            "event": message_type,
            "data": data,
            "error": f"Workflow execution error: {details}",
        }

    if message_type == "execution_interrupted":
        details = (
            f"Node Type: {data.get('node_type')}, "
            f"Node ID: {data.get('node_id')}"
        )
        return {
            "status": "error",
            "event": message_type,
            "data": data,
            "error": f"Workflow execution interrupted: {details}",
        }

    return None


def _history_terminal_state(history, prompt_id):
    """Recover a terminal execution state from a ComfyUI history response."""
    if not isinstance(history, dict):
        return None

    prompt_history = history.get(prompt_id)
    if not isinstance(prompt_history, dict):
        return None

    status = prompt_history.get("status", {})
    if not isinstance(status, dict):
        return None

    status_str = str(status.get("status_str", "")).lower()
    if status_str in ("error", "failed", "cancelled", "interrupted"):
        status_error = {
            "status": "error",
            "event": "history_status",
            "data": {"prompt_id": prompt_id},
            "error": f"Workflow execution ended with history status: {status_str}",
        }
    else:
        status_error = None

    # ComfyUI may emit the legacy executing(node=None) sentinel after failure.
    # A persisted failure always takes precedence over that success-shaped event.
    success_state = None
    messages = status.get("messages", [])
    if isinstance(messages, list):
        for history_message in reversed(messages):
            if not isinstance(history_message, (list, tuple)) or len(history_message) < 2:
                continue
            message_type, data = history_message[0], history_message[1]
            terminal_state = _execution_terminal_state(
                {"type": message_type, "data": data}, prompt_id
            )
            if terminal_state and terminal_state["status"] == "error":
                return terminal_state
            if terminal_state and success_state is None:
                success_state = terminal_state

    if status_error:
        return status_error
    if success_state:
        return success_state
    if status.get("completed") is True and status_str in (
        "",
        "success",
        "completed",
    ):
        return {
            "status": "success",
            "event": "history_status",
            "data": {"prompt_id": prompt_id},
        }

    return None


def wait_for_prompt_history(prompt_id, timeout_s=10, poll_interval_s=0.25):
    """Wait briefly for history persistence after a terminal WebSocket event."""
    deadline = time.monotonic() + max(0, timeout_s)
    last_history = {}

    while True:
        last_history = get_history(prompt_id)
        if prompt_id in last_history:
            return last_history

        remaining_s = deadline - time.monotonic()
        if remaining_s <= 0:
            return last_history

        time.sleep(min(max(0, poll_interval_s), remaining_s))


def get_image_data(filename, subfolder, image_type):
    """
    Fetch image bytes from the ComfyUI /view endpoint.

    Args:
        filename (str): The filename of the image.
        subfolder (str): The subfolder where the image is stored.
        image_type (str): The type of the image (e.g., 'output').

    Returns:
        bytes: The raw image data, or None if an error occurs.
    """
    print(
        f"worker-comfyui - Fetching image data: type={image_type}, subfolder={subfolder}, filename={filename}"
    )
    data = {"filename": filename, "subfolder": subfolder, "type": image_type}
    url_values = urllib.parse.urlencode(data)
    try:
        # Use requests for consistency and timeout
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched image data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching image data for {filename}")
        return None
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching image data for {filename}: {e}")
        return None
    except Exception as e:
        print(
            f"worker-comfyui - Unexpected error fetching image data for {filename}: {e}"
        )
        return None


def get_file_data(filename, subfolder, file_type):
    return get_image_data(filename, subfolder, file_type)


def _extension_for(filename):
    return os.path.splitext(filename or "")[1].lower()


def _media_kind_for_output(output_key, filename):
    extension = _extension_for(filename)
    if output_key in ("animated", "gifs", "videos") or extension in VIDEO_EXTENSIONS:
        return "videos"
    if output_key == "audio" or extension in AUDIO_EXTENSIONS:
        return "audio"
    if output_key == "images" or extension in IMAGE_EXTENSIONS:
        return "images"
    return "files"


def _is_text_artifact(filename):
    return _extension_for(filename) in TEXT_EXTENSIONS


def _normalize_artifact_key(file_type, subfolder, filename):
    path = os.path.normpath(os.path.join(subfolder or "", filename or ""))
    normalized_path = os.path.normcase(path).replace("\\", "/").replace(os.sep, "/")
    return f"{file_type or 'output'}:{normalized_path}"


def _safe_output_path(output_dir, *parts):
    base_path = os.path.realpath(output_dir)
    candidate = os.path.realpath(
        os.path.join(base_path, *[str(part) for part in parts if part])
    )
    try:
        if os.path.commonpath([base_path, candidate]) != base_path:
            return None
    except ValueError:
        return None
    return candidate


def validate_output_storage():
    """Reject partial S3 configuration before the SDK simulates an upload."""
    if not os.environ.get("BUCKET_ENDPOINT_URL"):
        return
    missing = [
        name for name in ("BUCKET_ACCESS_KEY_ID", "BUCKET_SECRET_ACCESS_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise ValueError("S3 upload configuration is missing: " + ", ".join(missing))
    endpoint = urllib.parse.urlparse(os.environ["BUCKET_ENDPOINT_URL"])
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname:
        raise ValueError("BUCKET_ENDPOINT_URL must be a valid HTTP(S) S3 endpoint")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("BUCKET_ENDPOINT_URL must not contain credentials or query parameters")


def _upload_output_bytes(job_id, filename, file_bytes):
    validate_output_storage()
    file_extension = os.path.splitext(filename)[1] or ".bin"
    temp_file_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=file_extension, delete=False) as temp_file:
            temp_file.write(file_bytes)
            temp_file_path = temp_file.name

        print(f"worker-comfyui - Uploading {filename} to S3...")
        if file_extension.lower() in IMAGE_EXTENSIONS:
            s3_url = rp_upload.upload_image(job_id, temp_file_path)
        else:
            # The legacy SDK helper hardcodes image/<extension>. Use its file
            # uploader for video/audio/text, retaining the same month bucket,
            # original Runpod job-id prefix and seven-day presigned URL.
            s3_url = rp_upload.upload_file_to_bucket(
                file_name=f"{str(uuid.uuid4())[:8]}{file_extension}",
                file_location=temp_file_path,
                prefix=str(job_id),
                extra_args={"ContentType": _guess_mime_type(filename)},
            )
        parsed_url = urllib.parse.urlparse(s3_url) if isinstance(s3_url, str) else None
        if not parsed_url or parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            raise RuntimeError("S3 upload did not return a usable HTTP(S) result URL")
        print(f"worker-comfyui - Uploaded {filename} to S3 successfully")
        return s3_url
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except OSError as rm_err:
                print(
                    f"worker-comfyui - Error removing temp file {temp_file_path}: {rm_err}"
                )


def _serialize_output_file(job_id, file_info, output_key):
    if not isinstance(file_info, dict):
        return None, f"Invalid file descriptor in output key {output_key}"
    filename = file_info.get("filename")
    subfolder = file_info.get("subfolder", "")
    file_type = file_info.get("type") or "output"
    kind = _media_kind_for_output(output_key, filename)

    if file_type in ("temp", "input"):
        print(
            f"worker-comfyui - Skipping {kind} preview {filename} of type '{file_type}'"
        )
        return None, None

    if not isinstance(filename, str) or not filename:
        return None, f"Skipping output in key {output_key} due to missing filename: {file_info}"

    file_bytes = get_file_data(filename, subfolder, file_type)
    if not file_bytes:
        return None, f"Failed to fetch output data for {filename} from /view endpoint."

    if not os.environ.get("BUCKET_ENDPOINT_URL") and len(file_bytes) > MAX_INLINE_OUTPUT_BYTES:
        return None, (
            f"Output {filename} exceeds MAX_INLINE_OUTPUT_BYTES "
            f"({MAX_INLINE_OUTPUT_BYTES} bytes). Configure BUCKET_ENDPOINT_URL "
            "and S3 credentials to deliver large outputs."
        )

    output_item = {
        "filename": filename,
        "media_type": kind[:-1] if kind.endswith("s") else kind,
    }

    if os.environ.get("BUCKET_ENDPOINT_URL"):
        s3_url = _upload_output_bytes(job_id, filename, file_bytes)
        output_item["type"] = "s3_url"
        output_item["data"] = s3_url
    else:
        output_item["type"] = "base64"
        output_item["data"] = base64.b64encode(file_bytes).decode("utf-8")
        if kind == "videos":
            output_item["warning"] = (
                "Video returned as base64 because BUCKET_ENDPOINT_URL is not configured."
            )

    if "format" in file_info:
        output_item["format"] = file_info["format"]
    if "frame_rate" in file_info:
        output_item["frame_rate"] = file_info["frame_rate"]

    return (kind, output_item), None


def _discover_text_artifacts_from_history(outputs):
    artifacts = []
    seen_artifacts = set()

    def walk(value, node_id, path):
        if isinstance(value, dict):
            filename = value.get("filename")
            if isinstance(filename, str) and _is_text_artifact(filename):
                file_type = value.get("type") or "output"
                if file_type != "temp":
                    subfolder = value.get("subfolder") or ""
                    dedupe_key = _normalize_artifact_key(file_type, subfolder, filename)
                    if dedupe_key not in seen_artifacts:
                        seen_artifacts.add(dedupe_key)
                        artifact = {
                            "source": "history",
                            "node_id": str(node_id),
                            "history_path": path,
                            "filename": filename,
                            "subfolder": subfolder,
                            "type": file_type,
                            "dedupe_key": dedupe_key,
                        }
                        artifacts.append(artifact)
                        print(
                            "worker-comfyui - Discovered text artifact in history: "
                            f"node={node_id}, path={path}, type={file_type}, "
                            f"subfolder={subfolder}, filename={filename}"
                        )

            for key, child in value.items():
                walk(child, node_id, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, node_id, f"{path}[{index}]")

    for node_id, node_output in outputs.items():
        walk(node_output, node_id, f"outputs.{node_id}")

    return artifacts


def _discover_text_artifacts_from_output_dir(output_dir, min_mtime=None):
    if not output_dir or not os.path.isdir(output_dir):
        print(
            "worker-comfyui - Text artifact scan skipped; output directory "
            f"does not exist: {output_dir}"
        )
        return []

    cutoff_mtime = None
    if min_mtime is not None:
        cutoff_mtime = max(0, min_mtime - TEXT_ARTIFACT_SCAN_MTIME_GRACE_S)

    candidates = []
    for root, dirs, files in os.walk(output_dir):
        dirs[:] = [dirname for dirname in dirs if not dirname.startswith(".")]
        for filename in files:
            if not _is_text_artifact(filename):
                continue

            file_path = os.path.join(root, filename)
            try:
                stat_result = os.stat(file_path)
            except OSError as stat_err:
                print(
                    "worker-comfyui - Could not stat text artifact candidate "
                    f"{file_path}: {stat_err}"
                )
                continue

            if cutoff_mtime is not None and stat_result.st_mtime < cutoff_mtime:
                continue

            candidates.append((stat_result.st_mtime, file_path))

    candidates.sort(key=lambda item: (item[0], item[1]))
    if len(candidates) > TEXT_ARTIFACT_SCAN_MAX_FILES:
        print(
            "worker-comfyui - Text artifact scan found "
            f"{len(candidates)} files; keeping the newest "
            f"{TEXT_ARTIFACT_SCAN_MAX_FILES}."
        )
        candidates = candidates[-TEXT_ARTIFACT_SCAN_MAX_FILES:]

    artifacts = []
    for _mtime, file_path in candidates:
        rel_path = os.path.relpath(file_path, output_dir)
        subfolder = os.path.dirname(rel_path)
        if subfolder == ".":
            subfolder = ""
        subfolder = subfolder.replace("\\", "/")
        filename = os.path.basename(file_path)
        dedupe_key = _normalize_artifact_key("output", subfolder, filename)
        artifacts.append(
            {
                "source": "filesystem",
                "path": file_path,
                "filename": filename,
                "subfolder": subfolder,
                "type": "output",
                "dedupe_key": dedupe_key,
            }
        )
        print(
            "worker-comfyui - Discovered text artifact on disk: "
            f"subfolder={subfolder}, filename={filename}, path={file_path}"
        )

    return artifacts


def _iter_text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_values(item)


def _discover_inline_text_outputs_from_history(outputs):
    artifacts = []

    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            continue

        for output_key in ("text", "texts"):
            if output_key not in node_output:
                continue

            for index, text in enumerate(_iter_text_values(node_output[output_key]), 1):
                filename = f"node_{node_id}_{output_key}_{index:05d}.txt"
                history_path = f"outputs.{node_id}.{output_key}[{index - 1}]"
                artifacts.append(
                    {
                        "source": "history_text",
                        "node_id": str(node_id),
                        "history_path": history_path,
                        "filename": filename,
                        "text": text,
                        "dedupe_key": f"history_text:{node_id}:{output_key}:{index}",
                    }
                )
                print(
                    "worker-comfyui - Discovered inline text output in history: "
                    f"node={node_id}, path={history_path}, filename={filename}, "
                    f"chars={len(text)}"
                )

    return artifacts


def _read_text_artifact_bytes(artifact, output_dir):
    if artifact["source"] == "history_text":
        return artifact.get("text", "").encode("utf-8")

    if artifact["source"] == "history":
        file_bytes = get_file_data(
            artifact["filename"],
            artifact.get("subfolder", ""),
            artifact.get("type") or "output",
        )
        if file_bytes is not None:
            return file_bytes

        if (artifact.get("type") or "output") == "output":
            local_path = _safe_output_path(
                output_dir, artifact.get("subfolder", ""), artifact["filename"]
            )
            if local_path and os.path.exists(local_path):
                print(
                    "worker-comfyui - Falling back to local text artifact read: "
                    f"{local_path}"
                )
                with open(local_path, "rb") as file_handle:
                    return file_handle.read()
        return None

    file_path = artifact.get("path")
    if not file_path:
        return None

    safe_path = _safe_output_path(output_dir, os.path.relpath(file_path, output_dir))
    if not safe_path or os.path.normcase(safe_path) != os.path.normcase(
        os.path.realpath(file_path)
    ):
        print(
            "worker-comfyui - Refusing to read text artifact outside output dir: "
            f"{file_path}"
        )
        return None

    with open(file_path, "rb") as file_handle:
        return file_handle.read()


def _decode_text_artifact(file_bytes):
    truncated = len(file_bytes) > TEXT_ARTIFACT_MAX_INLINE_BYTES
    data = file_bytes[:TEXT_ARTIFACT_MAX_INLINE_BYTES] if truncated else file_bytes

    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(encoding), truncated
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace"), truncated


def _find_existing_file_item(file_items, filename):
    for item in file_items or []:
        if item.get("filename") == filename:
            return item
    return None


def _serialize_text_artifact(job_id, artifact, output_dir, existing_file_items=None):
    filename = artifact["filename"]
    try:
        file_bytes = _read_text_artifact_bytes(artifact, output_dir)
    except OSError as read_err:
        return None, f"Failed to read text artifact {filename}: {read_err}"

    if file_bytes is None:
        return None, f"Failed to read text artifact {filename}."

    text, truncated = _decode_text_artifact(file_bytes)
    text_item = {"filename": filename, "text": text}
    if artifact.get("subfolder"):
        text_item["subfolder"] = artifact["subfolder"]
    if truncated:
        text_item["truncated"] = True

    serialized = {"files": [], "texts": [text_item]}
    existing_file_item = _find_existing_file_item(existing_file_items, filename)

    if os.environ.get("BUCKET_ENDPOINT_URL"):
        if existing_file_item and existing_file_item.get("type") == "s3_url":
            print(
                "worker-comfyui - Reusing existing uploaded text artifact: "
                f"{filename}"
            )
        else:
            try:
                s3_url = _upload_output_bytes(job_id, filename, file_bytes)
                file_item = {
                    "filename": filename,
                    "type": "s3_url",
                    "data": s3_url,
                    "media_type": "text",
                }
                if artifact.get("subfolder"):
                    file_item["subfolder"] = artifact["subfolder"]
                serialized["files"].append(file_item)
            except Exception as upload_err:
                # Inline text remains useful for diagnosis, but a required S3
                # upload failure must fail the job even when other files exist.
                return serialized, f"Failed to upload text artifact {filename}: {upload_err}"

    print(
        "worker-comfyui - Prepared text artifact: "
        f"filename={filename}, chars={len(text)}, truncated={truncated}"
    )
    return serialized, None


def collect_text_artifacts(
    outputs,
    job_id,
    output_dir=COMFY_OUTPUT_DIR,
    min_mtime=None,
    existing_file_items=None,
    scan_output_dir=True,
):
    text_outputs = {"files": [], "texts": []}
    errors = []
    seen_artifacts = set()

    artifacts = _discover_text_artifacts_from_history(outputs)
    artifacts.extend(_discover_inline_text_outputs_from_history(outputs))
    if scan_output_dir:
        artifacts.extend(_discover_text_artifacts_from_output_dir(output_dir, min_mtime))

    if not artifacts:
        print("worker-comfyui - No text artifacts discovered.")
        return text_outputs, errors

    existing_files = list(existing_file_items or [])
    for artifact in artifacts:
        dedupe_key = artifact["dedupe_key"]
        if dedupe_key in seen_artifacts:
            continue
        seen_artifacts.add(dedupe_key)

        serialized, error = _serialize_text_artifact(
            job_id, artifact, output_dir, existing_files + text_outputs["files"]
        )
        if error:
            print(f"worker-comfyui - {error}")
            errors.append(error)
        if not serialized:
            continue

        for file_item in serialized["files"]:
            if not _find_existing_file_item(
                existing_files + text_outputs["files"], file_item["filename"]
            ):
                text_outputs["files"].append(file_item)

        for text_item in serialized["texts"]:
            if not any(
                item.get("filename") == text_item["filename"]
                and item.get("text") == text_item["text"]
                for item in text_outputs["texts"]
            ):
                text_outputs["texts"].append(text_item)

    print(
        "worker-comfyui - Text artifact collection complete: "
        f"{len(text_outputs['files'])} file link(s), "
        f"{len(text_outputs['texts'])} inline text item(s)."
    )
    return text_outputs, errors


def collect_output_media(outputs, job_id):
    output_media = {"images": [], "videos": [], "audio": [], "files": [], "texts": []}
    errors = []
    seen_outputs = set()

    print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, dict):
            errors.append(f"Node {node_id} returned invalid output metadata")
            continue
        handled_keys = []

        for output_key in MEDIA_OUTPUT_KEYS:
            if output_key not in node_output:
                continue

            handled_keys.append(output_key)
            output_items = node_output.get(output_key) or []
            # Core animated image nodes emit `animated: [True]` as a UI hint,
            # alongside the actual file descriptors under `images`.
            if output_key == "animated" and (
                isinstance(output_items, bool)
                or (isinstance(output_items, (list, tuple)) and all(
                    isinstance(item, bool) for item in output_items
                ))
            ):
                continue
            if not isinstance(output_items, list):
                errors.append(
                    f"Node {node_id} output '{output_key}' was not a list: {output_items}"
                )
                continue

            print(
                f"worker-comfyui - Node {node_id} contains {len(output_items)} {output_key} item(s)"
            )

            for file_info in output_items:
                try:
                    if isinstance(file_info, dict) and file_info.get("filename"):
                        dedupe_key = _normalize_artifact_key(
                            file_info.get("type"), file_info.get("subfolder"),
                            file_info["filename"],
                        )
                        if dedupe_key in seen_outputs:
                            continue
                    else:
                        dedupe_key = None
                    result, error = _serialize_output_file(job_id, file_info, output_key)
                    if error:
                        print(f"worker-comfyui - {error}")
                        errors.append(error)
                        continue
                    if result:
                        kind, output_item = result
                        seen_outputs.add(dedupe_key)
                        output_media[kind].append(output_item)
                except Exception as e:
                    error_msg = (
                        f"Error processing output from node {node_id} "
                        f"key {output_key}: {e}"
                    )
                    print(f"worker-comfyui - {error_msg}")
                    errors.append(error_msg)

        other_keys = [k for k in node_output.keys() if k not in handled_keys]
        if other_keys:
            warn_msg = f"Node {node_id} produced unhandled output keys: {other_keys}."
            print(f"worker-comfyui - WARNING: {warn_msg}")

    return output_media, errors


def _find_credit_metadata(obj, path="history"):
    matches = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            child_path = f"{path}.{key_str}"
            if any(keyword in key_str.lower() for keyword in CREDIT_KEYWORDS):
                if isinstance(value, (str, int, float, bool)) or value is None:
                    matches.append({"path": child_path, "value": value})
                elif isinstance(value, (dict, list)):
                    matches.append({"path": child_path, "value": value})
            matches.extend(_find_credit_metadata(value, child_path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            matches.extend(_find_credit_metadata(value, f"{path}[{index}]"))
    elif isinstance(obj, str) and "credit" in obj.lower():
        amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:comfy\s*)?credits?", obj, re.I)
        matches.append(
            {
                "path": path,
                "value": obj,
                "amount": float(amount_match.group(1)) if amount_match else None,
            }
        )

    return matches


def extract_comfy_credit_usage(prompt_history):
    """
    Best-effort extraction of credit/cost metadata from ComfyUI history.

    Comfy.org API nodes may expose billing details differently by node/version, so
    the worker returns matching metadata paths instead of hard-coding one schema.
    """
    matches = _find_credit_metadata(prompt_history)
    if not matches:
        return {"available": False, "credits_spent": None, "details": []}

    numeric_values = [
        item["value"]
        for item in matches
        if isinstance(item.get("value"), (int, float))
        and any(word in item["path"].lower() for word in ("spent", "cost", "charged"))
    ]
    extracted_amounts = [
        item["amount"] for item in matches if isinstance(item.get("amount"), float)
    ]
    credits_spent = None
    if numeric_values:
        credits_spent = sum(float(value) for value in numeric_values)
    elif extracted_amounts:
        credits_spent = sum(extracted_amounts)

    return {
        "available": True,
        "credits_spent": credits_spent,
        "details": matches,
    }


def _credit_tracker_source(rows):
    sources = {str(row.get("source") or "").strip() for row in rows}
    sources.discard("")
    pricing_modes = {str(row.get("pricing_mode") or "").strip() for row in rows}
    pricing_modes.discard("")
    if len(sources) == 1:
        return f"credit_tracker:{next(iter(sources))}"
    if len(pricing_modes) == 1:
        return f"credit_tracker:{next(iter(pricing_modes))}"
    return "credit_tracker"


def _credit_tracker_row_to_node(row):
    return {
        "node_id": row.get("node_id") or None,
        "class_type": row.get("node_class_type") or "",
        "partner_node_name": row.get("partner_node_name") or "",
        "estimated_credits": round(float(row.get("estimated_credits") or 0.0), 4),
        "estimated_usd": round(float(row.get("estimated_usd") or 0.0), 4),
        "pricing_mode": row.get("pricing_mode") or "",
        "duration_seconds": float(row.get("duration_seconds") or 0.0),
        "resolution": row.get("resolution") or None,
        "model_name": row.get("model_name") or None,
        "node_title": row.get("node_title") or None,
        "quantity": int(row.get("quantity") or 0),
        "source": row.get("source") or "credit_tracker",
        "notes": row.get("notes") or "",
    }


def read_credit_tracker_usage(prompt_id, db_path=CREDIT_TRACKER_DB_PATH):
    """
    Read ComfyUI-Credit-Tracker rows for the current prompt, if the tracker is installed.

    The tracker runs inside ComfyUI and writes usage_log.db. Reading it here lets
    the serverless response use the same credit rows the local dashboard/logs show.
    """
    if not prompt_id or not db_path or not os.path.exists(db_path):
        return None

    connection = None
    try:
        connection = sqlite3.connect(db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                timestamp,
                project_name,
                user_name,
                workflow_name,
                partner_node_name,
                pricing_mode,
                quantity,
                duration_seconds,
                resolution,
                estimated_credits,
                estimated_usd,
                notes,
                prompt_id,
                node_id,
                node_class_type,
                node_title,
                model_name,
                input_summary,
                source,
                dedupe_key
            FROM credit_usage
            WHERE prompt_id = ?
            ORDER BY id ASC
            """,
            (str(prompt_id),),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"worker-comfyui - Credit tracker DB unavailable: {exc}")
        return None
    except Exception as exc:
        print(f"worker-comfyui - Could not read credit tracker DB: {exc}")
        return None
    finally:
        if connection is not None:
            connection.close()

    if not rows:
        return None

    row_dicts = [dict(row) for row in rows]
    nodes = [_credit_tracker_row_to_node(row) for row in row_dicts]
    total_credits = sum(node["estimated_credits"] for node in nodes)
    total_usd = sum(node["estimated_usd"] for node in nodes)

    return {
        "total_estimated_credits": round(total_credits, 4),
        "total_estimated_usd": round(total_usd, 4),
        "credits_per_usd": CREDITS_PER_USD,
        "source": _credit_tracker_source(row_dicts),
        "nodes": nodes,
        "prompt_id": str(prompt_id),
    }


def handler(job, *, scan_text_artifacts=True):
    """
    Handles a job using ComfyUI via websockets for status and image retrieval.

    Args:
        job (dict): A dictionary containing job details and input parameters.

    Returns:
        dict: A dictionary containing either an error message or a success status with generated images.
    """
    # ---------------------------------------------------------------------------
    # Network Volume Diagnostics (opt-in via NETWORK_VOLUME_DEBUG=true)
    # ---------------------------------------------------------------------------
    if is_network_volume_debug_enabled():
        run_network_volume_diagnostics()

    if not isinstance(job, dict) or not job.get("id") or "input" not in job:
        return {"error": "Job must include its Runpod 'id' and 'input'"}
    job_input = job["input"]
    job_id = job["id"]

    try:
        validate_output_storage()
    except ValueError as exc:
        return {"error": str(exc)}

    # Make sure that the input is valid
    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    # Extract validated data
    try:
        workflow = apply_prompt_overrides(
            validated_data["workflow"],
            prompt=validated_data.get("prompt"),
            negative_prompt=validated_data.get("negative_prompt"),
            prompt_node_id=validated_data.get("prompt_node_id"),
            negative_prompt_node_id=validated_data.get("negative_prompt_node_id"),
            prompt_replacements=validated_data.get("prompt_replacements"),
        )
    except ValueError as e:
        return {"error": str(e)}
    input_images = validated_data.get("images")
    input_videos = validated_data.get("videos")
    input_files = validated_data.get("files")

    # Make sure that the ComfyUI HTTP API is available before proceeding
    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {
            "error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
        }

    # Upload input images if they exist
    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            # Return upload errors
            return {
                "error": "Failed to upload one or more input images",
                "details": upload_result["details"],
            }

    # Upload input videos if they exist
    if input_videos:
        upload_result = upload_videos(input_videos)
        if upload_result["status"] == "error":
            return {
                "error": "Failed to upload one or more input videos",
                "details": upload_result["details"],
            }

    # Upload generic input files if they exist
    if input_files:
        upload_result = upload_files(input_files)
        if upload_result["status"] == "error":
            return {
                "error": "Failed to upload one or more input files",
                "details": upload_result["details"],
            }

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    job_started_at = None
    output_media = {"images": [], "videos": [], "audio": [], "files": [], "texts": []}
    errors = []
    comfy_credits = {"available": False, "credits_spent": None, "details": []}
    credit_usage = build_empty_credit_usage()
    execution_done = False
    queue_attempted = False
    started_node_ids = set()
    executed_node_ids = set()
    failed_node_ids = set()

    try:
        # Establish WebSocket connection
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print(f"worker-comfyui - Websocket connected")

        # Queue the workflow
        try:
            # Pass per-request API key if provided in input
            job_started_at = time.time()
            queue_attempted = True
            queued_workflow = queue_workflow(
                workflow,
                client_id,
                comfy_org_api_key=validated_data.get("comfy_org_api_key"),
            )
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            # For ValueError exceptions from queue_workflow, pass through the original message
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        # Wait for execution completion via WebSocket
        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        workflow_wait_started_at = time.monotonic()
        while True:
            raise_if_workflow_timed_out(prompt_id, workflow_wait_started_at)
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    terminal_state = _execution_terminal_state(message, prompt_id)
                    if terminal_state:
                        terminal_data = terminal_state["data"]
                        if terminal_state["status"] == "success":
                            print(
                                "worker-comfyui - Execution finished for prompt "
                                f"{prompt_id} ({terminal_state['event']})"
                            )
                            execution_done = True
                        else:
                            if terminal_data.get("node_id") is not None:
                                failed_node_ids.add(str(terminal_data.get("node_id")))
                            print(
                                "worker-comfyui - Execution failure received: "
                                f"{terminal_state['error']}"
                            )
                            errors.append(terminal_state["error"])
                        break

                    if message.get("type") == "status":
                        status_data = message.get("data", {}).get("status", {})
                        print(
                            f"worker-comfyui - Status update: {status_data.get('exec_info', {}).get('queue_remaining', 'N/A')} items remaining in queue"
                        )
                    elif message.get("type") == "executing":
                        data = message.get("data", {})
                        if (
                            data.get("prompt_id") == prompt_id
                            and data.get("node") is not None
                        ):
                            started_node_ids.add(str(data.get("node")))
                    elif message.get("type") == "executed":
                        data = message.get("data", {})
                        if (
                            data.get("prompt_id") == prompt_id
                            and data.get("node") is not None
                        ):
                            executed_node_ids.add(str(data.get("node")))
                else:
                    continue
            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                try:
                    fallback_history = get_history(prompt_id)
                except requests.RequestException as history_error:
                    print(
                        "worker-comfyui - Could not check history after websocket "
                        f"timeout: {history_error}"
                    )
                    continue

                terminal_state = _history_terminal_state(fallback_history, prompt_id)
                if not terminal_state:
                    continue

                terminal_data = terminal_state["data"]
                if terminal_state["status"] == "success":
                    print(
                        "worker-comfyui - Recovered workflow completion from "
                        f"history for prompt {prompt_id}."
                    )
                    execution_done = True
                else:
                    if terminal_data.get("node_id") is not None:
                        failed_node_ids.add(str(terminal_data.get("node_id")))
                    print(
                        "worker-comfyui - Recovered workflow failure from history: "
                        f"{terminal_state['error']}"
                    )
                    errors.append(terminal_state["error"])
                break
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    # Attempt to reconnect
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )

                    print(
                        "worker-comfyui - Resuming message listening after successful reconnect."
                    )
                    continue
                except (
                    websocket.WebSocketConnectionClosedException
                ) as reconn_failed_err:
                    # If _attempt_websocket_reconnect fails, it raises this exception
                    # Let this exception propagate to the outer handler's except block
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        # Fetch history even if there were execution errors, some outputs might exist
        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        history = wait_for_prompt_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return {
                    "error": error_msg,
                    "prompt_id": prompt_id,
                    "credit_usage": credit_usage,
                    "refresh_worker": True,
                }
            else:
                errors.append(error_msg)
                return {
                    "error": "Job processing failed, prompt ID not found in history.",
                    "details": errors,
                    "prompt_id": prompt_id,
                    "credit_usage": credit_usage,
                    "refresh_worker": True,
                }

        prompt_history = history.get(prompt_id, {})
        persisted_state = _history_terminal_state(history, prompt_id)
        if persisted_state and persisted_state["status"] == "error":
            execution_done = False
            if persisted_state["error"] not in errors:
                errors.append(persisted_state["error"])
        outputs = prompt_history.get("outputs", {})
        comfy_credits = extract_comfy_credit_usage(prompt_history)
        tracker_credit_usage = read_credit_tracker_usage(prompt_id)
        if tracker_credit_usage:
            credit_usage = tracker_credit_usage
        else:
            credit_usage = estimate_credit_usage(
                workflow,
                prompt_history,
                prompt_id,
                executed_node_ids=executed_node_ids,
                started_node_ids=started_node_ids,
                failed_node_ids=failed_node_ids,
                execution_success=execution_done and not errors,
            )
        print(
            "worker-comfyui - Credit usage: "
            + json.dumps(credit_usage, sort_keys=True)
        )

        no_outputs_warning = None
        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            no_outputs_warning = warning_msg

        output_media, media_errors = collect_output_media(outputs, job_id)
        errors.extend(media_errors)
        text_outputs, text_errors = collect_text_artifacts(
            outputs,
            job_id,
            output_dir=COMFY_OUTPUT_DIR,
            min_mtime=job_started_at,
            existing_file_items=output_media.get("files", []),
            scan_output_dir=scan_text_artifacts,
        )
        errors.extend(text_errors)
        for media_key, items in text_outputs.items():
            output_media.setdefault(media_key, [])
            for item in items:
                if item not in output_media[media_key]:
                    output_media[media_key].append(item)

        if (
            no_outputs_warning
            and not errors
            and sum(len(items) for items in output_media.values()) == 0
        ):
            errors.append(no_outputs_warning)

    except WorkflowExecutionTimeoutError as e:
        print(f"worker-comfyui - {e}")
        return {
            "error": str(e),
            "status": "timeout",
            "prompt_id": prompt_id,
            "credit_usage": credit_usage,
            "refresh_worker": True,
        }
    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {
            "error": f"WebSocket communication error: {e}",
            "prompt_id": prompt_id,
            "credit_usage": credit_usage,
            "refresh_worker": queue_attempted,
        }
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {
            "error": f"HTTP communication error with ComfyUI: {e}",
            "prompt_id": prompt_id,
            "credit_usage": credit_usage,
            "refresh_worker": queue_attempted,
        }
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {
            "error": str(e),
            "prompt_id": prompt_id,
            "credit_usage": credit_usage,
            "refresh_worker": queue_attempted,
        }
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {
            "error": f"An unexpected error occurred: {e}",
            "prompt_id": prompt_id,
            "credit_usage": credit_usage,
            "refresh_worker": queue_attempted,
        }
    finally:
        if ws:
            print(f"worker-comfyui - Closing websocket connection.")
            try:
                ws.close()
            except Exception as close_error:
                logger.warning("Could not close ComfyUI websocket: %s", close_error)

    final_result = {}
    final_result["success"] = True
    final_result["prompt_id"] = prompt_id

    for media_key, items in output_media.items():
        if items:
            final_result[media_key] = items

    final_result["comfy_credits"] = comfy_credits
    final_result["credit_usage"] = credit_usage

    if errors:
        final_result["errors"] = errors
        final_result["details"] = errors
        final_result["success"] = False
        final_result["error"] = "Job processing failed"
        final_result["refresh_worker"] = bool(prompt_id)
        print(f"worker-comfyui - Job failed: {errors}")
        return final_result

    output_count = sum(len(items) for items in output_media.values())

    if output_count == 0:
        print(
            f"worker-comfyui - Job completed successfully, but the workflow produced no output media."
        )
        final_result["status"] = "success_no_outputs"
        final_result["images"] = []
        final_result["videos"] = []

    print(f"worker-comfyui - Job completed. Returning {output_count} output item(s).")
    if REFRESH_WORKER:
        final_result["refresh_worker"] = True
    return final_result


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
