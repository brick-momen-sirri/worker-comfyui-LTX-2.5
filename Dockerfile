# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Every normal build includes all weights. --target runtime is for CI import checks.
FROM nvidia/cuda:13.0.2-base-ubuntu24.04@sha256:2ab6381d970b211fb93853796dc6707eb8a72575a375c422b17cf4d8b2641701 AS runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_INPUT=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    COMFY_LOG_LEVEL=INFO \
    COMFY_RESERVE_VRAM=2 \
    COMFY_STARTUP_TIMEOUT=300

# A dated Ubuntu snapshot pins OS package resolution as well as the base digest.
RUN sed -i -E 's#https?://(archive\.ubuntu\.com/ubuntu/?|security\.ubuntu\.com/ubuntu/?)#https://snapshot.ubuntu.com/ubuntu/20260910T000000Z/#g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && packages=(python3.12 python3.12-venv ca-certificates git ffmpeg \
       libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libsndfile1 \
       libtcmalloc-minimal4 openssh-server tini) \
    && apt-get -o Acquire::Check-Valid-Until=false --print-uris -qq --no-install-recommends install "${packages[@]}" > /tmp/apt-uris \
    && awk 'NF == 4 {print $1, "/var/cache/apt/archives/" $2, $4}' /tmp/apt-uris \
       | xargs -r -n3 -P8 /usr/lib/apt/apt-helper -o Acquire::Retries=3 download-file \
    && apt-get -o Acquire::Check-Valid-Until=false install -y --no-download --no-install-recommends "${packages[@]}" \
    && rm -f /tmp/apt-uris \
    && rm -rf /var/lib/apt/lists/* /etc/ssh/ssh_host_* \
    && python3.12 -m venv /opt/venv

COPY requirements/bootstrap.lock /requirements/bootstrap.lock
RUN python -m pip install --require-hashes -r /requirements/bootstrap.lock
COPY requirements /requirements
RUN uv pip sync --python /opt/venv/bin/python --require-hashes /requirements/runtime.lock \
    && python -m pip check

COPY sources.lock.json /sources.lock.json
COPY scripts/fetch_sources.py /scripts/fetch_sources.py
RUN python /scripts/fetch_sources.py /sources.lock.json

# Retain the original worker's bounded API-result download behavior.
COPY src/patch_comfy_api_download.py /scripts/patch_comfy_api_download.py
RUN python /scripts/patch_comfy_api_download.py /comfyui/comfy_api_nodes/util/download_helpers.py

COPY handler.py credit_estimator.py worker.py /
COPY src/network_volume.py /network_volume.py
COPY src/extra_model_paths.yaml /comfyui/extra_model_paths.yaml
COPY ltx_worker /ltx_worker
COPY workflows /workflows
COPY models /models
COPY scripts /scripts
COPY src/start.sh /start.sh
ARG MODEL_PROFILE=int8
RUN python /scripts/configure_profile.py --profile "${MODEL_PROFILE}" --root / \
    && sed -i 's/\r$//' /start.sh \
    && chmod 755 /start.sh \
    && mkdir -p /comfyui/input /comfyui/output /comfyui/temp \
    && python -m compileall -q /worker.py /handler.py /ltx_worker /scripts \
    && python /scripts/build_smoke.py \
    && rm -f /comfyui/custom_nodes/comfyui_credit_tracker/tracker_status.json \
       /comfyui/custom_nodes/comfyui_credit_tracker/usage_log.db \
       /comfyui/custom_nodes/comfyui_credit_tracker/usage_log.db-wal \
       /comfyui/custom_nodes/comfyui_credit_tracker/usage_log.db-shm

WORKDIR /
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=360s --retries=3 CMD ["python", "/scripts/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/start.sh"]

FROM runtime AS model-bundle
# Ephemeral secret mount: no ARG, ENV, copied credential, or HF login.
RUN --mount=type=secret,id=hf_token,required=true \
    python /scripts/download_models.py --token-file /run/secrets/hf_token \
    && python /scripts/download_models.py --verify-only --size-only

FROM model-bundle AS final
ARG MODEL_PROFILE=int8
LABEL org.opencontainers.image.title="Runpod ComfyUI LTX 2.5 worker" \
      io.ltx.model-profile="${MODEL_PROFILE}" \
      org.opencontainers.image.description="Complete pinned offline model bundle with preserved original worker result delivery"
