"""FastSHAP surrogate explainer (Phase E).

Production FastSHAP uses a trained neural surrogate to produce Shapley
values in a single forward pass. Until that ONNX artifact ships, this
module provides a deterministic linear-attribution explainer that
matches the same input/output contract so the backend integration is
frozen.

Output contract — **must match** ``backend/app/services/explain.py``:

    [
        {"feature": "velocity_count", "contribution": 12.3, "source": "model",
         "description": "velocity_count=3"},
        ...
    ]

Latency target: ≤3ms on a typical feature vector (≤20 keys). The
backend enforces a 10ms total budget including HTTP round-trip.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-feature weights used by the linear surrogate. Loaded lazily from
# ``AFDS_FASTSHAP_WEIGHTS_PATH`` (JSON: ``{"feature": weight, ...}``).
# When absent we fall back to unit weights, which degrades gracefully
# to a magnitude ranking (identical to the pre-Phase-E behaviour).
_WEIGHTS: dict[str, float] | None = None
_WEIGHTS_LOADED = False


def _load_weights() -> dict[str, float]:
    global _WEIGHTS, _WEIGHTS_LOADED
    if _WEIGHTS_LOADED:
        return _WEIGHTS or {}
    _WEIGHTS_LOADED = True
    path = os.getenv("AFDS_FASTSHAP_WEIGHTS_PATH", "").strip()
    if not path:
        return {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            return {}
        _WEIGHTS = {
            str(k): float(v)
            for k, v in payload.items()
            if isinstance(v, (int, float))
        }
        return _WEIGHTS or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("FastSHAP weights unavailable (%s); using unit weights", exc)
        return {}


def _reset_for_tests() -> None:
    global _WEIGHTS, _WEIGHTS_LOADED
    _WEIGHTS = None
    _WEIGHTS_LOADED = False


def _describe(feature: str, value: float) -> str:
    # Human-readable label for the UI. Kept purely cosmetic so the
    # contract value is ``contribution`` only.
    if math.isclose(value, 0.0, abs_tol=1e-9):
        return f"{feature}=0"
    if abs(value) >= 100:
        return f"{feature}={value:.0f}"
    if abs(value) >= 1:
        return f"{feature}={value:.2f}"
    return f"{feature}={value:.3f}"


def explain(features: dict[str, Any], top_k: int = 5) -> tuple[list[dict[str, Any]], str]:
    """Return ``(reason_codes, mode)``.

    ``mode`` is ``"fastshap-linear"`` when weights are loaded, else
    ``"symbolic-magnitude"`` (deterministic fallback). Both variants
    respect the unified schema.
    """
    weights = _load_weights()
    mode = "fastshap-linear" if weights else "symbolic-magnitude"

    contributions: list[tuple[str, float, float]] = []  # (feature, contribution, value)
    for name, raw_value in (features or {}).items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        w = weights.get(name, 1.0) if weights else 1.0
        contrib = value * w
        contributions.append((name, contrib, value))

    contributions.sort(key=lambda t: abs(t[1]), reverse=True)
    trimmed = contributions[: max(top_k, 0)] if top_k > 0 else contributions

    return (
        [
            {
                "feature": name,
                "contribution": round(contrib, 4),
                "source": "model",
                "description": _describe(name, value),
            }
            for name, contrib, value in trimmed
        ],
        mode,
    )


def explain_timed(
    features: dict[str, Any], top_k: int = 5
) -> tuple[list[dict[str, Any]], str, float]:
    t0 = time.perf_counter()
    codes, mode = explain(features, top_k=top_k)
    return codes, mode, round((time.perf_counter() - t0) * 1000.0, 3)
