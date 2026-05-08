"""
AFDS - Autonomous Fraud Defense System
FastAPI Backend Application

High-speed orchestration layer bridging the exchange
and the fraud defense engine.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.kafka import close_kafka_producer
from app.core.middleware import RequestIdMiddleware, RateLimitMiddleware, AuditLogMiddleware
from app.core.security import get_current_user
from app.routers import transactions, alerts, sanctions, dashboard, realtime, rules, auth, reporting, network, rule_chat, device_fingerprint, enrichment, debug
from app.services.feature_store import get_feature_store, shutdown_feature_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AFDS Backend starting up...")
    # Warm the online feature store singleton (Redis or in-memory fallback).
    try:
        store = await get_feature_store()
        logger.info("feature_store backend=%s", store.backend)
    except Exception as exc:  # noqa: BLE001 - never block startup on the store
        logger.warning("feature_store init failed: %s", exc)
    # Idempotently seed the default detection rules into Postgres.
    try:
        from app.routers.rules import seed_default_rules
        await seed_default_rules()
    except Exception as exc:  # noqa: BLE001
        logger.warning("rules seed failed: %s", exc)
    yield
    logger.info("AFDS Backend shutting down...")
    await close_kafka_producer()
    await shutdown_feature_store()


app = FastAPI(
    title="AFDS - Autonomous Fraud Defense System",
    description="Real-time financial transaction monitoring, risk scoring, and automated interdiction API.",
    version="1.0.0",
    lifespan=lifespan,
)

# Middleware (order matters — outermost first)
app.add_middleware(AuditLogMiddleware)
app.add_middleware(RateLimitMiddleware, requests_per_minute=120)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
# Public (no auth): login, health, metrics, and the public device-fingerprint beacon.
app.include_router(auth.router, prefix="/api/v1")
app.include_router(device_fingerprint.router, prefix="/api/v1")

# Authenticated (Bearer JWT or X-API-Key required):
PROTECTED = [Depends(get_current_user)]
app.include_router(transactions.router, prefix="/api/v1", dependencies=PROTECTED)
app.include_router(alerts.router,       prefix="/api/v1", dependencies=PROTECTED)
app.include_router(sanctions.router,    prefix="/api/v1", dependencies=PROTECTED)
app.include_router(dashboard.router,    prefix="/api/v1", dependencies=PROTECTED)
app.include_router(realtime.router,     prefix="/api/v1", dependencies=PROTECTED)
app.include_router(rules.router,        prefix="/api/v1", dependencies=PROTECTED)
app.include_router(reporting.router,    prefix="/api/v1", dependencies=PROTECTED)
app.include_router(network.router,      prefix="/api/v1", dependencies=PROTECTED)
app.include_router(rule_chat.router,    prefix="/api/v1", dependencies=PROTECTED)
app.include_router(enrichment.router,   prefix="/api/v1", dependencies=PROTECTED)
app.include_router(debug.router,        prefix="/api/v1", dependencies=PROTECTED)

# Prometheus metrics — exposed at /metrics
from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, include_in_schema=False)


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "afds-backend"}


@app.get("/health/feature-store")
async def health_feature_store():
    """Diagnostic endpoint for the Phase A1 online feature store."""
    store = await get_feature_store()
    return await store.health()
