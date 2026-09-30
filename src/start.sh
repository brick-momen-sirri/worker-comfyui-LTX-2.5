#!/usr/bin/env bash
set -Eeuo pipefail

cleanup() {
    trap - EXIT TERM INT
    for process in "${WORKER_PID:-}" "${COMFY_PID:-}"; do
        if [[ -n "$process" ]]; then kill "$process" 2>/dev/null || true; fi
    done
    for ((attempt=0; attempt<15; attempt++)); do
        alive=false
        for process in "${WORKER_PID:-}" "${COMFY_PID:-}"; do
            if [[ -n "$process" ]] && kill -0 "$process" 2>/dev/null; then alive=true; fi
        done
        if [[ "$alive" == "false" ]]; then break; fi
        sleep 1
    done
    for process in "${WORKER_PID:-}" "${COMFY_PID:-}"; do
        if [[ -n "$process" ]]; then kill -KILL "$process" 2>/dev/null || true; fi
    done
    wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Preserve the opt-in SSH and local Runpod API behavior of the existing worker.
if [[ -n "${PUBLIC_KEY:-}" ]]; then
    install -d -m 700 /root/.ssh
    printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    ssh-keygen -A
    service ssh start
fi

TCMALLOC="$(ldconfig -p | awk '!seen && /libtcmalloc_minimal.so.4 / {print $NF; seen=1}')"
if [[ -n "$TCMALLOC" ]]; then export LD_PRELOAD="${LD_PRELOAD:+$LD_PRELOAD:}$TCMALLOC"; fi

# No package installer, Manager bootstrap, HF login, or model download runs here.
python /scripts/download_models.py --verify-only --size-only
python /scripts/gpu_preflight.py

COMFY_ARGS=(--disable-auto-launch --disable-metadata --listen 127.0.0.1
    --verbose "${COMFY_LOG_LEVEL:-INFO}" --log-stdout --cache-none
    --input-directory "${COMFY_INPUT_DIR:-/comfyui/input}"
    --output-directory "${COMFY_OUTPUT_DIR:-/comfyui/output}"
    --reserve-vram "${COMFY_RESERVE_VRAM:-2}")
if [[ "${SERVE_API_LOCALLY:-false}" == "true" ]]; then
    # ComfyUI remains on localhost; the Runpod development API binds port 8000.
    WORKER_ARGS=(--rp_serve_api --rp_api_host=0.0.0.0)
else
    WORKER_ARGS=()
fi
if [[ -n "${COMFY_EXTRA_ARGS:-}" ]]; then
    read -r -a EXTRA_ARGS <<< "$COMFY_EXTRA_ARGS"
    COMFY_ARGS+=("${EXTRA_ARGS[@]}")
fi

cd /comfyui
# Registration must be from this process, not a previous worker boot.
rm -f /comfyui/custom_nodes/comfyui_credit_tracker/tracker_status.json
python -u main.py "${COMFY_ARGS[@]}" &
COMFY_PID=$!
printf '%s\n' "$COMFY_PID" > /tmp/comfyui.pid
cd /
python /scripts/wait_for_comfy.py --timeout "${COMFY_STARTUP_TIMEOUT:-300}" --validate-workflows
python -u /worker.py "${WORKER_ARGS[@]}" &
WORKER_PID=$!
printf '%s\n' "$WORKER_PID" > /tmp/worker.pid

# A dead ComfyUI process must terminate the Runpod worker as well.
set +e
wait -n "$COMFY_PID" "$WORKER_PID"
STATUS=$?
set -e
exit "$STATUS"
