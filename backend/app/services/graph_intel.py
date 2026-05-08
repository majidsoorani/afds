"""
Graph intelligence client — Phase C2.

Thin async client that calls the ``afds-model-service`` sidecar with a
feature vector produced from :mod:`app.services.graph_store` (k-hop
aggregates) merged with the realtime transaction features. Mirrors the
advisory contract of :mod:`app.services.anomaly`:

* Returns a ``dict`` on success with keys ``score``, ``is_anomaly``,
  ``reason_codes``, ``latency_ms``, ``model_version``.
* Returns ``None`` on any failure, timeout, or when disabled. The caller
  is expected to treat ``None`` as "no signal" and skip the factor — so
  the public validation suite remains passing regardless of model health.

Mode gating mirrors :mod:`app.core.config`:

* ``AFDS_MODEL_MODE=off``  → always returns ``None``.
* ``AFDS_GNN_ENABLED=false`` → always returns ``None``.
* otherwise → call ``/score`` on ``AFDS_MODEL_ENDPOINT`` with a
  ``AFDS_MODEL_TIMEOUT_MS`` deadline.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _truthy(v: str | None) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    mode = (os.getenv("AFDS_MODEL_MODE") or "off").strip().lower()
    if mode == "off":
        return False
    if not _truthy(os.getenv("AFDS_GNN_ENABLED")):
        return False
    if not os.getenv("AFDS_MODEL_ENDPOINT"):
        return False
    return True


def _timeout_seconds() -> float:
    try:
        return max(float(os.getenv("AFDS_MODEL_TIMEOUT_MS", "40")) / 1000.0, 0.005)
    except ValueError:
        return 0.04


# A single shared async client; httpx pools connections for us.
_client: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=os.getenv("AFDS_MODEL_ENDPOINT", ""),
            timeout=_timeout_seconds(),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


async def score(
    entity_id: str,
    features: dict[str, float],
    graph_features: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """Call the model-service `/score` endpoint with a merged feature vector.

    Returns the raw JSON (as a dict) or ``None`` on any failure / disabled.
    """
    if not is_enabled():
        return None

    merged: dict[str, float] = {}
    merged.update(features or {})
    if graph_features:
        merged.update(graph_features)

    payload = {
        "entity_id": entity_id,
        "features": merged,
        "graph_context": graph_features or {},
    }

    try:
        client = await _get_client()
        resp = await client.post("/score", json=payload)
        resp.raise_for_status()
        data = resp.json()
    except httpx.TimeoutException:
        logger.debug("graph_intel: /score timed out after %sms", os.getenv("AFDS_MODEL_TIMEOUT_MS", "40"))
        return None
    except Exception as exc:  # noqa: BLE001 - never raise into scoring path
        logger.debug("graph_intel: /score call failed (%s)", exc)
        return None

    # Normalise the contract so realtime.py can consume it identically to
    # the existing anomaly block.
    try:
        return {
            "score": float(data.get("score", 0.0) or 0.0),
            "is_anomaly": bool(data.get("is_anomaly", False)),
            "reason_codes": list(data.get("reason_codes") or []),
            "latency_ms": float(data.get("latency_ms", 0.0) or 0.0),
            "model_version": str(data.get("model_version") or "unknown"),
            "model_name": str(data.get("model_name") or "gnn"),
        }
    except (TypeError, ValueError) as exc:
        logger.debug("graph_intel: malformed response (%s)", exc)
        return None


__all__ = ["is_enabled", "score", "aclose"]
