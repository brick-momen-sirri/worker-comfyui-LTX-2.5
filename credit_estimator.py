from __future__ import annotations

import re
from typing import Any


CREDITS_PER_USD = 211.0


NANO_BANANA_CLASS_TYPES = {
    "GeminiImageNode",
    "GeminiImage2Node",
    "GeminiNanoBanana2",
}

SEEDANCE_CLASS_TYPES = {
    "ByteDance2TextToVideoNode",
    "ByteDance2FirstLastFrameNode",
    "ByteDance2ReferenceNode",
}

KLING_CLASS_TYPES = {
    "KlingVideoNode",
    "KlingImage2VideoNode",
    "KlingFirstLastFrameNode",
    "KlingTextToVideoNode",
    "KlingCameraControlI2VNode",
    "KlingStartEndFrameNode",
    "KlingDualCharacterNode",
    "KlingAvatarNode",
}

RUNTIME_PRICE_KEY_RE = re.compile(
    r"(price|cost|charge|charged|billing|bill|spent|usd|dollar)", re.I
)
USD_KEY_RE = re.compile(
    r"(usd|price_usd|cost_usd|amount_usd|estimated_usd|actual_usd|dollar)", re.I
)
CREDIT_KEY_RE = re.compile(r"(credit|credits)", re.I)
USD_TEXT_RE = re.compile(
    r"(?:\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:usd|dollars?))",
    re.I,
)
CREDIT_TEXT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:comfy\s*)?credits?", re.I)


def build_empty_credit_usage(source: str = "none") -> dict[str, Any]:
    return {
        "total_estimated_credits": 0.0,
        "total_estimated_usd": 0.0,
        "credits_per_usd": CREDITS_PER_USD,
        "source": source,
        "nodes": [],
    }


def estimate_credit_usage(
    workflow: dict[str, Any] | None,
    prompt_history: dict[str, Any] | None = None,
    prompt_id: str | None = None,
    *,
    executed_node_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    started_node_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    failed_node_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    execution_success: bool = True,
) -> dict[str, Any]:
    """Estimate Comfy API-node credit usage for one serverless prompt run."""
    workflow = workflow or {}
    prompt_history = prompt_history or {}
    executed = {str(item) for item in (executed_node_ids or set()) if item is not None}
    started = {str(item) for item in (started_node_ids or set()) if item is not None}
    failed = {str(item) for item in (failed_node_ids or set()) if item is not None}

    runtime_rows = _runtime_price_rows(prompt_history, workflow)
    runtime_node_ids = {
        row["node_id"] for row in runtime_rows if row.get("node_id") is not None
    }

    fallback_rows: list[dict[str, Any]] = []
    if not _has_unscoped_runtime_price(runtime_rows):
        for node_id, node in _workflow_nodes(workflow):
            if node_id in runtime_node_ids:
                continue
            estimated = _estimate_node_price(node_id, node)
            if estimated is None:
                continue

            node_executed = _should_count_fallback_node(
                node_id,
                execution_success=execution_success,
                executed_node_ids=executed,
                started_node_ids=started,
            )
            if node_executed:
                fallback_rows.append(estimated)
            elif node_id in started or node_id in failed:
                zero_row = dict(estimated)
                zero_row["estimated_credits"] = 0.0
                zero_row["estimated_usd"] = 0.0
                zero_row["pricing_mode"] = "failed_before_charge"
                fallback_rows.append(zero_row)

    rows = _dedupe_rows(runtime_rows + fallback_rows)
    total_credits = sum(_as_float(row.get("estimated_credits"), 0.0) for row in rows)
    total_usd = sum(_as_float(row.get("estimated_usd"), 0.0) for row in rows)

    if runtime_rows and fallback_rows:
        source = "mixed"
    elif runtime_rows:
        source = "runtime_price"
    elif fallback_rows:
        source = "estimated"
    else:
        source = "none"

    return {
        "total_estimated_credits": round(total_credits, 4),
        "total_estimated_usd": round(total_usd, 4),
        "credits_per_usd": CREDITS_PER_USD,
        "source": source,
        "nodes": rows,
        "prompt_id": prompt_id,
    }


def _workflow_nodes(workflow: dict[str, Any]):
    for node_id, node in workflow.items():
        if isinstance(node, dict):
            yield str(node_id), node


def _should_count_fallback_node(
    node_id: str,
    *,
    execution_success: bool,
    executed_node_ids: set[str],
    started_node_ids: set[str],
) -> bool:
    if not execution_success:
        return False
    if executed_node_ids:
        return node_id in executed_node_ids
    if started_node_ids:
        return node_id in started_node_ids
    return True


def _estimate_node_price(node_id: str, node: dict[str, Any]) -> dict[str, Any] | None:
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}

    if class_type == "KlingVideoNode" or class_type in KLING_CLASS_TYPES:
        return _estimate_kling_row(node_id, class_type, inputs)

    if class_type in SEEDANCE_CLASS_TYPES:
        duration = _duration_seconds(inputs, default=5.0)
        credits = 20.0 * duration
        return _row(
            node_id,
            class_type,
            "Seedance",
            credits / CREDITS_PER_USD,
            "pricing_table",
            duration_seconds=duration,
            resolution=_resolution(inputs),
            model_name=_model_name(inputs, "seedance"),
        )

    if class_type in NANO_BANANA_CLASS_TYPES:
        return _row(
            node_id,
            class_type,
            "Nano Banana",
            14.7 / CREDITS_PER_USD,
            "pricing_table",
            model_name=_model_name(inputs, "nano-banana"),
        )

    return None


def _estimate_kling_row(
    node_id: str, class_type: str, inputs: dict[str, Any]
) -> dict[str, Any]:
    model_name = _model_name(inputs, "kling")
    resolution = _kling_resolution(inputs)
    duration = _kling_multishot_duration(inputs, default=5.0)
    generate_audio = _truthy(_input_value(inputs, ("generate_audio",), True))

    if class_type == "KlingVideoNode" or "v3" in model_name.lower():
        audio_key = "on" if generate_audio else "off"
        rates = {
            "4k": {"off": 0.42, "on": 0.42},
            "1080p": {"off": 0.112, "on": 0.168},
            "720p": {"off": 0.084, "on": 0.126},
        }
        usd = rates.get(resolution, rates["1080p"]).get(audio_key, 0.168) * duration
        partner = "Kling"
    else:
        rates = {"4k": 0.28, "1080p": 0.14, "720p": 0.07}
        usd = rates.get(resolution, rates["1080p"]) * duration
        partner = "Kling"

    return _row(
        node_id,
        class_type,
        partner,
        usd,
        "estimated_formula",
        duration_seconds=duration,
        resolution=resolution,
        model_name=model_name,
    )


def _row(
    node_id: str | None,
    class_type: str,
    partner_node_name: str,
    usd: float,
    pricing_mode: str,
    *,
    duration_seconds: float | None = None,
    resolution: str | None = None,
    model_name: str | None = None,
    source_path: str | None = None,
) -> dict[str, Any]:
    credits = usd * CREDITS_PER_USD
    row = {
        "node_id": node_id,
        "class_type": class_type,
        "partner_node_name": partner_node_name,
        "estimated_credits": round(credits, 4),
        "estimated_usd": round(usd, 4),
        "pricing_mode": pricing_mode,
        "duration_seconds": duration_seconds,
        "resolution": resolution,
        "model_name": model_name,
    }
    if source_path:
        row["source_path"] = source_path
    return row


def _duration_seconds(inputs: dict[str, Any], default: float = 0.0) -> float:
    for key in (
        "duration_seconds",
        "duration",
        "clip_duration",
        "seconds",
        "model.duration",
    ):
        if key in inputs:
            return max(_as_float(inputs.get(key), default), 0.0)
    return default


def _kling_multishot_duration(inputs: dict[str, Any], default: float) -> float:
    multi_shot = str(_input_value(inputs, ("multi_shot",), "")).strip().lower()
    if multi_shot and multi_shot not in {"disabled", "off", "false", "none"}:
        total = 0.0
        for key, value in inputs.items():
            if re.fullmatch(r"multi_shot\.storyboard_\d+_duration", str(key)):
                total += max(_as_float(value, 0.0), 0.0)
        if total > 0:
            return total
    return _duration_seconds(inputs, default=default)


def _resolution(inputs: dict[str, Any]) -> str | None:
    value = _input_value(inputs, ("resolution", "model.resolution"), None)
    if value is None:
        return None
    return str(value).strip().lower()


def _kling_resolution(inputs: dict[str, Any]) -> str:
    value = str(_resolution(inputs) or "1080p").strip().lower()
    if value in {"1080", "1080p", "hd"}:
        return "1080p"
    if value in {"720", "720p", "std", "standard"}:
        return "720p"
    if value in {"4k", "2160", "2160p"}:
        return "4k"
    return value


def _model_name(inputs: dict[str, Any], default: str) -> str:
    value = _input_value(inputs, ("model_name", "model", "api", "engine"), default)
    return str(value or default)


def _input_value(inputs: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if name in inputs:
            return inputs[name]
    return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", "none", ""}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _runtime_price_rows(
    prompt_history: dict[str, Any], workflow: dict[str, Any]
) -> list[dict[str, Any]]:
    paid_nodes = {
        node_id: node
        for node_id, node in _workflow_nodes(workflow)
        if _estimate_node_price(node_id, node) is not None
    }
    matches: list[dict[str, Any]] = []
    _collect_runtime_prices(prompt_history, "$", set(paid_nodes.keys()), matches)

    if not matches:
        return []

    rows: list[dict[str, Any]] = []
    for match in matches:
        node_id = match.get("node_id")
        if not node_id and len(paid_nodes) == 1:
            node_id = next(iter(paid_nodes.keys()))
        node = paid_nodes.get(str(node_id)) if node_id is not None else {}
        class_type = str(node.get("class_type") or "unknown")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}

        usd = match.get("usd")
        credits = match.get("credits")
        if usd is None and credits is not None:
            usd = _as_float(credits) / CREDITS_PER_USD
        if usd is None:
            continue

        rows.append(
            _row(
                str(node_id) if node_id is not None else None,
                class_type,
                _partner_name_for_class(class_type),
                _as_float(usd),
                "runtime_price",
                duration_seconds=_duration_seconds(inputs, default=0.0)
                if inputs
                else None,
                resolution=_resolution(inputs),
                model_name=_model_name(inputs, class_type) if inputs else None,
                source_path=match.get("path"),
            )
        )
    return rows


def _collect_runtime_prices(
    obj: Any,
    path: str,
    workflow_node_ids: set[str],
    matches: list[dict[str, Any]],
    node_hint: str | None = None,
) -> None:
    if isinstance(obj, dict):
        current_hint = _node_hint_from_dict(obj, workflow_node_ids) or node_hint
        for key, value in obj.items():
            key_str = str(key)
            next_hint = key_str if key_str in workflow_node_ids else current_hint
            child_path = f"{path}.{key_str}"

            if _looks_like_runtime_price_key(key_str):
                numeric = _as_float(value, None)
                if numeric is not None:
                    entry = {"path": child_path, "node_id": next_hint}
                    if USD_KEY_RE.search(key_str):
                        entry["usd"] = numeric
                    elif CREDIT_KEY_RE.search(key_str):
                        entry["credits"] = numeric
                    else:
                        entry["usd"] = numeric
                    matches.append(entry)
                elif isinstance(value, str):
                    _append_price_from_text(value, child_path, next_hint, matches)

            if isinstance(value, str) and RUNTIME_PRICE_KEY_RE.search(value):
                _append_price_from_text(value, child_path, next_hint, matches)

            _collect_runtime_prices(
                value, child_path, workflow_node_ids, matches, next_hint
            )
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _collect_runtime_prices(
                value, f"{path}[{index}]", workflow_node_ids, matches, node_hint
            )
    elif isinstance(obj, str) and RUNTIME_PRICE_KEY_RE.search(obj):
        _append_price_from_text(obj, path, node_hint, matches)


def _looks_like_runtime_price_key(key: str) -> bool:
    lowered = key.lower()
    if "vram" in lowered or "memory" in lowered:
        return False
    return bool(RUNTIME_PRICE_KEY_RE.search(lowered))


def _append_price_from_text(
    value: str, path: str, node_id: str | None, matches: list[dict[str, Any]]
) -> None:
    usd_match = USD_TEXT_RE.search(value)
    if usd_match:
        amount = usd_match.group(1) or usd_match.group(2)
        matches.append({"path": path, "node_id": node_id, "usd": float(amount)})
        return
    credit_match = CREDIT_TEXT_RE.search(value)
    if credit_match:
        matches.append(
            {"path": path, "node_id": node_id, "credits": float(credit_match.group(1))}
        )


def _node_hint_from_dict(obj: dict[str, Any], workflow_node_ids: set[str]) -> str | None:
    for key in ("node_id", "node", "id"):
        value = obj.get(key)
        if value is not None and str(value) in workflow_node_ids:
            return str(value)
    return None


def _partner_name_for_class(class_type: str) -> str:
    if "kling" in class_type.lower():
        return "Kling"
    if class_type in SEEDANCE_CLASS_TYPES:
        return "Seedance"
    if class_type in NANO_BANANA_CLASS_TYPES:
        return "Nano Banana"
    return class_type or "Unknown"


def _has_unscoped_runtime_price(rows: list[dict[str, Any]]) -> bool:
    return any(row.get("node_id") is None for row in rows)


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for row in rows:
        key = (
            row.get("node_id"),
            row.get("pricing_mode"),
            row.get("source_path") or row.get("class_type"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped
