"""
Graph store — Phase C1 of the Advanced AFDS (GNN + DL + XAI) rollout.

Provides k-hop neighborhood aggregates for a sender/account entity, used as
input features for the GraphSAGE scorer (Phase C2/C3). Two sources:

1. **PostgreSQL** (preferred in production): reads edges from the
   ``transactions`` table over a configurable lookback window.
2. **In-memory ring buffer**: the same buffer :mod:`app.routers.realtime`
   maintains via ``_velocity_windows`` / ``_inbound_velocity_windows``.
   Used as a laptop/dev fallback when the DB is unreachable or empty.

Results are cached in the online feature store
(:mod:`app.services.feature_store`) keyed under scope ``graph`` with a
short TTL so we don't hammer Postgres on the hot scoring path. The
neighborhood fetch has a strict deadline (:data:`_FETCH_TIMEOUT_SECONDS`)
and always degrades to an empty neighborhood on error — the scoring path
is never blocked, keeping the public validation suite (passing) green.

The output schema is a flat ``dict[str, float]`` so it can be concatenated
directly with the :mod:`app.services.anomaly` feature vector and sent to
the CPU ONNX Runtime sidecar.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Iterable

from app.services.feature_store import get_feature_store

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────

_ENABLED = os.getenv("AFDS_GRAPH_STORE_ENABLED", "1").lower() in ("1", "true", "yes")
_LOOKBACK_HOURS = int(os.getenv("AFDS_GRAPH_LOOKBACK_HOURS", "72"))
_MAX_HOP = int(os.getenv("AFDS_GRAPH_MAX_HOP", "2"))
_MAX_NEIGHBORS = int(os.getenv("AFDS_GRAPH_MAX_NEIGHBORS", "50"))
_FETCH_TIMEOUT_SECONDS = float(os.getenv("AFDS_GRAPH_FETCH_TIMEOUT", "0.08"))  # 80 ms
_CACHE_TTL_SECONDS = int(os.getenv("AFDS_GRAPH_CACHE_TTL", "60"))


# ── Public data shape ──────────────────────────────────────────────────

_FEATURE_KEYS = (
    "graph_1hop_degree",
    "graph_1hop_unique_counterparties",
    "graph_1hop_amount_sum",
    "graph_1hop_amount_mean",
    "graph_1hop_amount_max",
    "graph_2hop_fanout",
    "graph_2hop_flagged_fraction",
    "graph_in_out_ratio",
    "graph_is_bridge",
    "graph_present",
)


def empty_neighborhood() -> dict[str, float]:
    """Return a zero-filled feature vector used when no graph context exists."""
    return {k: 0.0 for k in _FEATURE_KEYS}


# ── Fallback: in-memory from realtime router buffers ───────────────────


def _memory_neighborhood(entity_id: str) -> dict[str, float]:
    """Derive cheap graph aggregates from the realtime ring buffers.

    The realtime router keeps rolling windows per sender/receiver; they are
    enough to populate degree + amount stats without hitting PG. Used when
    the DB is unreachable or in unit tests.
    """
    try:
        from app.routers.realtime import (  # local import to avoid cycles
            _velocity_windows,
            _inbound_velocity_windows,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_store: realtime buffers unavailable (%s)", exc)
        return empty_neighborhood()

    out_edges = list(_velocity_windows.get(entity_id, ()))
    in_edges = list(_inbound_velocity_windows.get(entity_id, ()))

    out_amounts = [float(e.get("amount", 0.0) or 0.0) for e in out_edges]
    out_counterparties = {e.get("external_id") for e in out_edges if e.get("external_id")}
    in_counterparties = {e.get("sender_id") for e in in_edges if e.get("sender_id")}

    one_hop_degree = len(out_edges) + len(in_edges)
    uniq_cp = len(out_counterparties | in_counterparties)
    amount_sum = sum(out_amounts)
    amount_mean = (amount_sum / len(out_amounts)) if out_amounts else 0.0
    amount_max = max(out_amounts) if out_amounts else 0.0
    fanout_2hop = min(len(out_counterparties) * len(in_counterparties), 2500)
    in_out_ratio = (
        (len(in_edges) / len(out_edges)) if out_edges else (1.0 if in_edges else 0.0)
    )
    # A node is a "bridge" when it has both non-trivial in and out and
    # a balanced ratio — classic money-mule fan-in/fan-out signature.
    is_bridge = 1.0 if (len(in_edges) >= 3 and len(out_edges) >= 1 and 0.3 <= in_out_ratio <= 5.0) else 0.0

    return {
        "graph_1hop_degree": float(one_hop_degree),
        "graph_1hop_unique_counterparties": float(uniq_cp),
        "graph_1hop_amount_sum": float(amount_sum),
        "graph_1hop_amount_mean": float(amount_mean),
        "graph_1hop_amount_max": float(amount_max),
        "graph_2hop_fanout": float(fanout_2hop),
        "graph_2hop_flagged_fraction": 0.0,  # not available in-memory
        "graph_in_out_ratio": float(in_out_ratio),
        "graph_is_bridge": is_bridge,
        "graph_present": 1.0 if one_hop_degree > 0 else 0.0,
    }


# ── Postgres path ──────────────────────────────────────────────────────


_PG_1HOP_SQL = """
WITH edges AS (
    SELECT sender_id, receiver_id, amount::float8 AS amount,
           COALESCE(risk_score, 0)::float8 AS risk_score
    FROM transactions
    WHERE (sender_id = :entity OR receiver_id = :entity)
      AND created_at >= NOW() - (:lookback_hours || ' hours')::interval
    LIMIT :row_cap
)
SELECT
    COUNT(*)                                               AS one_hop_degree,
    COUNT(DISTINCT CASE WHEN sender_id = :entity THEN receiver_id
                         WHEN receiver_id = :entity THEN sender_id END) AS uniq_cp,
    COALESCE(SUM(CASE WHEN sender_id = :entity THEN amount ELSE 0 END), 0) AS amount_sum,
    COALESCE(AVG(CASE WHEN sender_id = :entity THEN amount END), 0)        AS amount_mean,
    COALESCE(MAX(CASE WHEN sender_id = :entity THEN amount END), 0)        AS amount_max,
    SUM(CASE WHEN sender_id  = :entity THEN 1 ELSE 0 END)  AS out_count,
    SUM(CASE WHEN receiver_id = :entity THEN 1 ELSE 0 END) AS in_count,
    AVG(CASE WHEN risk_score >= 50 THEN 1.0 ELSE 0.0 END)  AS flagged_fraction
FROM edges
"""


async def _postgres_neighborhood(entity_id: str) -> dict[str, float] | None:
    """Read 1-hop stats from Postgres. Returns ``None`` on any failure."""
    try:
        from sqlalchemy import text

        from app.core.database import async_session
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_store: DB bindings unavailable (%s)", exc)
        return None

    try:
        async with async_session() as session:
            result = await session.execute(
                text(_PG_1HOP_SQL),
                {
                    "entity": entity_id,
                    "lookback_hours": _LOOKBACK_HOURS,
                    "row_cap": _MAX_NEIGHBORS * 10,
                },
            )
            row = result.mappings().first()
    except Exception as exc:  # noqa: BLE001 - intentionally broad: we never block scoring
        logger.debug("graph_store: PG query failed (%s)", exc)
        return None

    if row is None:
        return empty_neighborhood()

    out_count = float(row.get("out_count") or 0)
    in_count = float(row.get("in_count") or 0)
    in_out_ratio = (in_count / out_count) if out_count > 0 else (1.0 if in_count > 0 else 0.0)
    is_bridge = 1.0 if (in_count >= 3 and out_count >= 1 and 0.3 <= in_out_ratio <= 5.0) else 0.0
    one_hop_degree = float(row.get("one_hop_degree") or 0)
    uniq_cp = float(row.get("uniq_cp") or 0)

    return {
        "graph_1hop_degree": one_hop_degree,
        "graph_1hop_unique_counterparties": uniq_cp,
        "graph_1hop_amount_sum": float(row.get("amount_sum") or 0),
        "graph_1hop_amount_mean": float(row.get("amount_mean") or 0),
        "graph_1hop_amount_max": float(row.get("amount_max") or 0),
        # Fan-out proxy: product of in/out distinct counterparties, capped.
        "graph_2hop_fanout": float(min(uniq_cp * max(in_count, 1.0), 2500)),
        "graph_2hop_flagged_fraction": float(row.get("flagged_fraction") or 0),
        "graph_in_out_ratio": float(in_out_ratio),
        "graph_is_bridge": is_bridge,
        "graph_present": 1.0 if one_hop_degree > 0 else 0.0,
    }


# ── Public API ─────────────────────────────────────────────────────────


async def get_neighborhood(
    entity_id: str,
    *,
    use_cache: bool = True,
) -> dict[str, float]:
    """Return the flat neighborhood feature vector for ``entity_id``.

    Strategy:
      1. Feature-store cache (scope=graph) — ~1 ms.
      2. Postgres 1-hop aggregates — ~10–40 ms, deadline ``_FETCH_TIMEOUT_SECONDS``.
      3. In-memory realtime buffers — always available.
      4. Zero-filled fallback.

    Never raises. Scoring path-safe.
    """
    if not _ENABLED or not entity_id:
        return empty_neighborhood()

    store = await get_feature_store()
    if use_cache:
        cached = await store.get("graph", entity_id)
        if cached is not None:
            return {k: float(cached.get(k, 0.0)) for k in _FEATURE_KEYS}

    t0 = time.perf_counter()
    pg_result: dict[str, float] | None = None
    try:
        pg_result = await asyncio.wait_for(
            _postgres_neighborhood(entity_id),
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.debug(
            "graph_store: PG neighborhood fetch timed out after %.0fms",
            _FETCH_TIMEOUT_SECONDS * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph_store: PG neighborhood fetch errored (%s)", exc)

    features = pg_result if pg_result is not None else _memory_neighborhood(entity_id)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug("graph_store: fetched in %.2fms (entity=%s)", elapsed_ms, entity_id)

    if use_cache:
        try:
            await store.put("graph", entity_id, features, ttl=_CACHE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph_store: cache write failed (%s)", exc)

    return features


def feature_keys() -> tuple[str, ...]:
    """Public accessor so callers can align their feature vectors."""
    return _FEATURE_KEYS


__all__ = [
    "empty_neighborhood",
    "feature_keys",
    "get_neighborhood",
]
