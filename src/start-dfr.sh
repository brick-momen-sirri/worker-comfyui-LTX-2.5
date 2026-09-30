#!/usr/bin/env bash
set -Eeuo pipefail

# Every model and dependency is installed during the image build.
python /scripts/download_models.py --manifest /models-manifest.json \
    --root "${DFR_MODEL_ROOT:-/models/ltx25}" --verify-only --size-only
python /scripts/dfr_preflight.py

ARGS=()
if [[ "${SERVE_API_LOCALLY:-false}" == "true" ]]; then
    ARGS=(--rp_serve_api --rp_api_host=0.0.0.0)
fi
printf '%s\n' "$$" > /tmp/dfr-worker.pid
exec python -u /dfr_worker.py "${ARGS[@]}"
