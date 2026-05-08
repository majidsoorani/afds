"""afds-model-service — CPU-only FastAPI inference sidecar (Phase B)."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

from .registry import ModelRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MODEL_REGISTRY_PATH = os.getenv("AFDS_MODEL_REGISTRY", "./models")
VAE_MODEL_NAME = os.getenv("AFDS_VAE_MODEL_NAME", "vae")
GNN_MODEL_NAME = os.getenv("AFDS_GNN_MODEL_NAME", "gnn")

_registry = ModelRegistry(MODEL_REGISTRY_PATH)
_reloader_task = None


from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: D401
    """Start the zero-downtime reloader on startup; cancel on shutdown."""
    global _reloader_task
    try:
        from .reloader import start_reloader
        _reloader_task = start_reloader(_registry, registry_root=MODEL_REGISTRY_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reloader failed to start: %s", exc)
    try:
        yield
    finally:
        if _reloader_task is not None:
            _reloader_task.cancel()
            try:
                await _reloader_task
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(
    title="afds-model-service",
    description="CPU-only ONNX Runtime inference for the Advanced AFDS rollout.",
    version="0.1.0",
    lifespan=lifespan,
)


class ScoreRequest(BaseModel):
    features: dict[str, float] = Field(..., description="Flat feature vector.")
    entity_id: str | None = Field(None, description="Optional sender / account id.")
    graph_context: dict[str, Any] | None = Field(
        None, description="Optional k-hop graph aggregates (Phase C)."
    )


class ScoreResponse(BaseModel):
    model_name: str
    model_version: str
    score: float
    is_anomaly: bool
    reason_codes: list[dict[str, Any]]
    latency_ms: float


class ExplainRequest(BaseModel):
    features: dict[str, float]
    top_k: int = 5


class ExplainResponse(BaseModel):
    reason_codes: list[dict[str, Any]]
    mode: str
    latency_ms: float


def _symbolic_reason_codes(features: dict[str, float], top_k: int = 5) -> list[dict[str, Any]]:
    """Deterministic neuro-symbolic reason codes.

    We rank raw feature magnitudes as a stand-in until the FastSHAP surrogate
    ships in a later sprint. This is cheap, stable, and always available, so
    the backend can populate ``reason_codes`` inline with ≤1 ms overhead.
    """
    ranked = sorted(
        features.items(),
        key=lambda kv: abs(float(kv[1])),
        reverse=True,
    )[:top_k]
    return [
        {"feature": name, "value": float(value), "contribution": float(value)}
        for name, value in ranked
    ]


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "service": "afds-model-service",
        "registry": _registry.describe(),
    }


@app.get("/models")
async def list_models() -> dict[str, Any]:
    return _registry.describe()


@app.post("/score", response_model=ScoreResponse)
async def score(req: ScoreRequest) -> ScoreResponse:
    """Score a feature vector with the VAE (and optionally the GNN)."""
    t0 = time.perf_counter()
    model = _registry.get(VAE_MODEL_NAME)
    try:
        score_val, is_anom = model.score(req.features)
    except Exception as exc:  # noqa: BLE001
        logger.warning("score failed for model=%s: %s", VAE_MODEL_NAME, exc)
        raise HTTPException(status_code=503, detail=f"inference_error:{exc}") from exc

    reason_codes = _symbolic_reason_codes(req.features)
    latency_ms = (time.perf_counter() - t0) * 1000
    return ScoreResponse(
        model_name=VAE_MODEL_NAME,
        model_version=model.version,
        score=float(score_val),
        is_anomaly=bool(is_anom),
        reason_codes=reason_codes,
        latency_ms=round(latency_ms, 3),
    )


@app.post("/explain", response_model=ExplainResponse)
async def explain(req: ExplainRequest) -> ExplainResponse:
    """Unified FastSHAP explainer.

    Uses the linear FastSHAP surrogate when weights are configured via
    ``AFDS_FASTSHAP_WEIGHTS_PATH``; otherwise returns a deterministic
    magnitude-ranked explanation. Both paths emit the contract schema
    consumed by ``backend/app/services/explain.py``.
    """
    from .explain import explain_timed  # lazy import: keep /health cold-start fast

    codes, mode, latency_ms = explain_timed(req.features, top_k=req.top_k)
    return ExplainResponse(
        reason_codes=codes,
        mode=mode,
        latency_ms=latency_ms,
    )


Instrumentator(
    should_group_status_codes=True,
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, include_in_schema=False)
