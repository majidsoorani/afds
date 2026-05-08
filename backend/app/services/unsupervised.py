"""
Unsupervised anomaly detection (Phase D) — VAE via ONNX Runtime.

Advisory, laptop-safe, and off by default. Replaces the Gap-10
``IForest`` fit-on-synthetic only when explicitly enabled via
``AFDS_VAE_ENABLED=true`` AND a valid ONNX model is reachable on
disk at ``AFDS_VAE_MODEL_PATH`` (plus an optional calibration
JSON at ``AFDS_VAE_CALIBRATION_PATH``).

Contract mirrors :func:`app.services.anomaly.score_features`:

    {
        "reconstruction_error": float,
        "percentile": float in [0,1],
        "anomaly_score": float in [0,100],
        "is_anomaly": bool,
        "source": "onnx.vae",
        "model_version": str,
    }

Failure modes (never raise): missing deps, missing file, bad schema,
corrupt calibration — all return ``None`` from :func:`score_features`
so the caller can transparently fall back to IForest.

Training scaffold lives at ``data-pipeline/ml/train_vae_ieee_cis.py``;
it writes ``model.onnx`` + ``calibration.json`` into a versioned
directory that can be pointed at directly via ``AFDS_VAE_MODEL_PATH``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Feature contract — must match the training script's feature order.
_FEATURE_NAMES = [
    "amount_log",
    "velocity_count",
    "hour_of_day",
    "is_weekend",
    "entity_risk",
    "ip_risk",
    "phone_risk",
    "email_risk",
    "cop_reason",
    "geo_mismatch",
]

# Lazy-loaded global state guarded by ``_LOCK`` for thread-safety.
_LOCK = threading.Lock()
_SESSION: Any | None = None
_INPUT_NAME: str | None = None
_OUTPUT_NAMES: list[str] | None = None
_CALIBRATION: list[float] | None = None  # sorted recon-errors for percentile
_MODEL_VERSION: str = "unknown"
_LOAD_ATTEMPTED = False
_LOAD_FAILED = False


def is_enabled() -> bool:
    return os.getenv("AFDS_VAE_ENABLED", "false").lower() in ("1", "true", "yes")


def _reset_for_tests() -> None:
    """Test helper: clear cached state so env vars can be re-read."""
    global _SESSION, _INPUT_NAME, _OUTPUT_NAMES, _CALIBRATION
    global _MODEL_VERSION, _LOAD_ATTEMPTED, _LOAD_FAILED
    with _LOCK:
        _SESSION = None
        _INPUT_NAME = None
        _OUTPUT_NAMES = None
        _CALIBRATION = None
        _MODEL_VERSION = "unknown"
        _LOAD_ATTEMPTED = False
        _LOAD_FAILED = False


def _load() -> bool:
    """Lazy-load ONNX session + calibration. Idempotent. Returns True on success."""
    global _SESSION, _INPUT_NAME, _OUTPUT_NAMES, _CALIBRATION
    global _MODEL_VERSION, _LOAD_ATTEMPTED, _LOAD_FAILED
    if _LOAD_ATTEMPTED:
        return _SESSION is not None

    with _LOCK:
        if _LOAD_ATTEMPTED:  # re-check after acquiring lock
            return _SESSION is not None
        _LOAD_ATTEMPTED = True

        model_path = os.getenv("AFDS_VAE_MODEL_PATH", "").strip()
        if not model_path or not os.path.isfile(model_path):
            logger.info("VAE disabled: AFDS_VAE_MODEL_PATH not set or missing")
            _LOAD_FAILED = True
            return False

        try:
            import onnxruntime as ort  # type: ignore
        except ImportError:
            logger.warning("onnxruntime not installed; VAE disabled")
            _LOAD_FAILED = True
            return False

        try:
            sess = ort.InferenceSession(
                model_path, providers=["CPUExecutionProvider"]
            )
            inputs = sess.get_inputs()
            outputs = sess.get_outputs()
            if not inputs or not outputs:
                raise ValueError("ONNX model missing input/output metadata")
            _SESSION = sess
            _INPUT_NAME = inputs[0].name
            _OUTPUT_NAMES = [o.name for o in outputs]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load VAE ONNX from %s: %s", model_path, exc)
            _LOAD_FAILED = True
            return False

        # Calibration (sorted reconstruction errors from the training set) is
        # optional. When absent, we fall back to a sigmoid of the raw error.
        calib_path = os.getenv("AFDS_VAE_CALIBRATION_PATH", "").strip()
        if not calib_path:
            # Default: sibling calibration.json next to the model.
            calib_path = os.path.join(
                os.path.dirname(model_path), "calibration.json"
            )
        if os.path.isfile(calib_path):
            try:
                with open(calib_path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                errs = payload.get("reconstruction_errors") or []
                errs = sorted(float(e) for e in errs if isinstance(e, (int, float)))
                if errs:
                    _CALIBRATION = errs
                _MODEL_VERSION = str(payload.get("version", "unknown"))
            except Exception as exc:  # noqa: BLE001
                logger.debug("VAE calibration read failed (%s); using sigmoid", exc)

        logger.info(
            "VAE loaded: path=%s version=%s calibration=%d",
            model_path,
            _MODEL_VERSION,
            len(_CALIBRATION) if _CALIBRATION else 0,
        )
        return True


def _features_to_row(features: dict[str, Any]) -> list[float]:
    amount = float(features.get("amount", 0) or 0)
    return [
        math.log1p(max(amount, 0.0)),
        float(features.get("velocity_count", 0) or 0),
        float(features.get("hour_of_day", 12) or 12),
        float(features.get("is_weekend", 0) or 0),
        float(features.get("entity_risk", 0) or 0),
        float(features.get("ip_risk", 0) or 0),
        float(features.get("phone_risk", 0) or 0),
        float(features.get("email_risk", 0) or 0),
        float(features.get("cop_reason", 0) or 0),
        float(features.get("geo_mismatch", 0) or 0),
    ]


def _percentile(error: float) -> float:
    """Map a reconstruction error to [0,1] using the calibration CDF.

    Falls back to a sigmoid when calibration is unavailable.
    """
    if _CALIBRATION:
        # Binary search for rank.
        lo, hi = 0, len(_CALIBRATION)
        while lo < hi:
            mid = (lo + hi) // 2
            if _CALIBRATION[mid] < error:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(_CALIBRATION)
    # Sigmoid fallback (centred on 1.0 error which is a sane VAE prior).
    return 1.0 / (1.0 + math.exp(-(error - 1.0)))


def score_features(features: dict[str, Any]) -> dict[str, Any] | None:
    """Return VAE anomaly score, or ``None`` if unavailable / disabled.

    Callers should treat ``None`` as "fall back to the IForest baseline".
    """
    if not is_enabled():
        return None
    if not _load():
        return None

    try:
        import numpy as np  # type: ignore

        row = _features_to_row(features)
        x = np.asarray([row], dtype=np.float32)
        outputs = _SESSION.run(_OUTPUT_NAMES, {_INPUT_NAME: x})  # type: ignore[union-attr]

        # Two supported output shapes:
        #   1. Single output = reconstructed row (N, F) — compute MSE in Python.
        #   2. Two outputs = (recon, error) where ``error`` is scalar per row.
        if len(outputs) >= 2 and np.asarray(outputs[1]).size == 1:
            error = float(np.asarray(outputs[1]).ravel()[0])
        else:
            recon = np.asarray(outputs[0], dtype=np.float32)
            if recon.shape != x.shape:
                # Model may return flattened encoding rather than a reconstruction;
                # fall back to the L2 norm of the output as a proxy.
                error = float(np.linalg.norm(recon))
            else:
                error = float(np.mean((recon - x) ** 2))

        pct = _percentile(error)
        return {
            "reconstruction_error": round(error, 6),
            "percentile": round(pct, 4),
            "anomaly_score": round(pct * 100.0, 2),
            "is_anomaly": pct >= 0.95,
            "features_used": _FEATURE_NAMES,
            "source": "onnx.vae",
            "model_version": _MODEL_VERSION,
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("VAE scoring failed: %s", exc)
        return None
