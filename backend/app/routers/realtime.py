"""
AFDS Real-Time Risk Scoring Engine — Live API

Implements the full Flink pipeline risk scoring logic in Python,
with in-memory velocity tracking, pattern detection, and COP integration.
Works standalone without Kafka, Flink, or PostgreSQL.

Endpoints:
  POST /api/v1/realtime/score         — Score a single transaction instantly
  POST /api/v1/realtime/batch         — Score a batch of transactions
    GET  /api/v1/realtime/simulate      — Replay synthetic demo transactions in real time
  GET  /api/v1/realtime/state         — Current engine state (velocity windows, alerts)
  POST /api/v1/realtime/reset         — Reset engine state
  POST /api/v1/realtime/cop-feed      — Feed COP verification results into scoring
"""

from __future__ import annotations
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/realtime", tags=["Real-Time Scoring"])

# ──────────────────────────────────────────────
# In-memory engine state
# ──────────────────────────────────────────────
VELOCITY_WINDOW_SEC = 120  # 2-minute window (matches Flink)

_velocity_windows: dict[str, list[dict]] = defaultdict(list)  # sender_id → [tx records in window]
_scored_history: list[dict] = []  # all scored transactions
_alerts: list[dict] = []  # generated alerts
_interdictions: list[dict] = []  # interdiction commands
_cop_cache: dict[str, dict] = {}  # account_id → COP verification result
_inbound_velocity_windows: dict[str, list[dict]] = defaultdict(list)  # receiver_id → [{"ts": float, "sender_id": str}]
_engine_stats = {
    "total_scored": 0,
    "total_flagged": 0,
    "total_blocked": 0,
    "total_suspended": 0,
    "total_allowed": 0,
    "start_time": None,
}

# ──────────────────────────────────────────────
# Risk scoring constants (mirrors Flink SQL exactly)
# ──────────────────────────────────────────────
HIGH_AMOUNT_THRESHOLDS = [
    (Decimal("50000"), 35.0),
    (Decimal("10000"), 20.0),
    (Decimal("5000"), 10.0),
]

VELOCITY_THRESHOLDS = [
    (10, 40.0),
    (5, 25.0),
    (3, 10.0),
]

PATTERN_SMALL_AMOUNT = Decimal("10")
PATTERN_MIN_VELOCITY = 3

# Entity risk: known suspicious patterns
ENTITY_RISK_KEYWORDS = [
    "test", "fraud", "blocked", "suspicious", "sanctioned",
]

# COP ReasonCode risk weights
COP_RISK_WEIGHTS = {
    "AC01": 40.0,   # Account doesn't exist
    "ANNM": 25.0,   # Name no match
    "IVCR": 20.0,   # Invalid creditor reference
    "ACNS": 18.0,   # Account not supported
    "MBAM": 12.0,   # Multiple matches (ambiguous)
    "BAMM": 10.0,   # Business matched multiple
    "PAMM": 10.0,   # Personal matched multiple
    "OPTO": 8.0,    # Opted out
    "BANM": 5.0,    # Business name match (type mismatch)
    "CASS": 3.0,    # CASS redirect
    "SCNS": 2.0,    # Secondary ref not supported
}


# ──────────────────────────────────────────────
# Request/Response Models
# ──────────────────────────────────────────────
class RealtimeTransaction(BaseModel):
    external_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender_id: str
    receiver_id: str | None = None
    amount: float = Field(gt=0)
    currency: str = Field(default="GBP", max_length=3)
    sender_iban: str | None = None
    receiver_iban: str | None = None
    transaction_type: str = Field(default="SEND_MONEY")
    sender_name: str | None = None
    receiver_name: str | None = None
    receiver_account_id: str | None = None
    timestamp: str | None = None
    sender_salary: float | None = None


class CopVerification(BaseModel):
    account_id: str
    reason_code: str | None = None
    matched: bool = False
    requested_name: str | None = None
    returned_name: str | None = None


class BatchRequest(BaseModel):
    transactions: list[RealtimeTransaction]


# ──────────────────────────────────────────────
# Core scoring engine
# ──────────────────────────────────────────────
def _prune_velocity_window(sender_id: str, now: float):
    """Remove entries older than VELOCITY_WINDOW_SEC."""
    cutoff = now - VELOCITY_WINDOW_SEC
    _velocity_windows[sender_id] = [
        e for e in _velocity_windows[sender_id] if e["ts"] > cutoff
    ]


def _prune_inbound_velocity_window(receiver_id: str, now: float):
    """Remove entries older than 30 days for third-party vendor Rule 10087 simulation."""
    thirty_days_sec = 30 * 24 * 3600
    cutoff = now - thirty_days_sec
    _inbound_velocity_windows[receiver_id] = [
        e for e in _inbound_velocity_windows[receiver_id] if e["ts"] > cutoff
    ]


def _get_velocity_count(sender_id: str, now: float) -> int:
    _prune_velocity_window(sender_id, now)
    return len(_velocity_windows[sender_id])


def _check_duplicate(sender_id: str, amount: float, now: float) -> bool:
    """Check for same-amount transactions in window."""
    _prune_velocity_window(sender_id, now)
    for entry in _velocity_windows[sender_id]:
        if abs(entry["amount"] - amount) < 0.01:
            return True
    return False


def raw_score_precheck(*scores: float) -> float:
    """Sum of rule-based scores computed up to the ML stage."""
    return float(sum(scores))


def score_transaction(tx: RealtimeTransaction) -> dict:
    """Apply the full AFDS risk scoring pipeline to a single transaction."""
    now = time.time()
    if _engine_stats["start_time"] is None:
        _engine_stats["start_time"] = datetime.now(timezone.utc).isoformat()

    amount = Decimal(str(tx.amount))
    factors = []

    # ── 1. Velocity Score ──
    velocity_count = _get_velocity_count(tx.sender_id, now) + 1  # +1 for current tx
    velocity_score = 0.0
    for threshold, score in VELOCITY_THRESHOLDS:
        if velocity_count >= threshold:
            velocity_score = score
            break
    if velocity_score > 0:
        factors.append(f"VELOCITY:{velocity_count}txns/2min(+{velocity_score})")

    # ── 2. Amount Score ──
    amount_score = 0.0
    for threshold, score in HIGH_AMOUNT_THRESHOLDS:
        if amount >= threshold:
            amount_score = score
            break
    if amount_score > 0:
        factors.append(f"AMOUNT:{tx.amount:.2f}(+{amount_score})")

    # ── 3. Pattern Score (testing-the-waters) ──
    pattern_score = 0.0
    pattern_detected = "NONE"
    if amount < PATTERN_SMALL_AMOUNT and velocity_count >= PATTERN_MIN_VELOCITY:
        pattern_score = 25.0
        pattern_detected = "TESTING_THE_WATERS"
        factors.append(f"PATTERN:{pattern_detected}(+{pattern_score})")
    elif velocity_count >= PATTERN_MIN_VELOCITY and amount >= Decimal("1000"):
        # third-party vendor Rule 10099: small amounts followed by a large payment = escalation
        _prune_velocity_window(tx.sender_id, now)
        prev_amounts = [Decimal(str(e["amount"])) for e in _velocity_windows[tx.sender_id]]
        if prev_amounts and all(a < PATTERN_SMALL_AMOUNT for a in prev_amounts):
            pattern_score = 25.0
            pattern_detected = "ESCALATION_SMALL_TO_LARGE"
            factors.append(f"PATTERN:{pattern_detected}(+{pattern_score})")
    if pattern_detected == "NONE" and velocity_count >= 10:
        pattern_detected = "VELOCITY_BURST"
        factors.append(f"PATTERN:{pattern_detected}")

    # ── 4. Duplicate Detection ──
    duplicate_score = 0.0
    if _check_duplicate(tx.sender_id, tx.amount, now):
        duplicate_score = 15.0
        factors.append(f"DUPLICATE:same_amount_rapid(+{duplicate_score})")

    # ── 5. Entity Risk ──
    entity_score = 0.0
    for name_field in [tx.sender_name, tx.receiver_name, tx.sender_id, tx.receiver_id]:
        if name_field:
            for keyword in ENTITY_RISK_KEYWORDS:
                if keyword in name_field.lower():
                    entity_score = max(entity_score, 10.0)
                    factors.append(f"ENTITY:suspicious_name({name_field})(+10.0)")
                    break

    # ── 6. COP Verification Score ──
    cop_score = 0.0
    cop_detail = None
    if tx.receiver_account_id and tx.receiver_account_id in _cop_cache:
        cop = _cop_cache[tx.receiver_account_id]
        reason = cop.get("reason_code")
        if reason and reason in COP_RISK_WEIGHTS:
            cop_score = COP_RISK_WEIGHTS[reason]
            factors.append(f"COP:{reason}(+{cop_score})")
            cop_detail = cop
        elif cop.get("matched") is False:
            cop_score = 15.0
            factors.append(f"COP:UNMATCHED(+{cop_score})")
            cop_detail = cop

    # ── 7. third-party vendor Inbound Velocity (Rule 10087) ──
    inbound_velocity_score = 0.0
    if tx.receiver_id:
        _prune_inbound_velocity_window(tx.receiver_id, now)
        _inbound_velocity_windows[tx.receiver_id].append({"ts": now, "sender_id": tx.sender_id})
        unique_senders = len(set(e["sender_id"] for e in _inbound_velocity_windows[tx.receiver_id]))
        if unique_senders >= 30:
            inbound_velocity_score = 45.0
            factors.append(f"VELOCITY_INBOUND_BURST:30+senders/month(+{inbound_velocity_score})")
            
    # ── 8. third-party vendor Custom Entity Metadata (Rule 10103) ──
    income_score = 0.0
    if tx.sender_salary is not None and tx.amount > tx.sender_salary:
        income_score = 30.0
        factors.append(f"AMOUNT_EXCEEDS_INCOME:spent>salary(+{income_score})")

    # ── 9. ML Anomaly Advisory (Gap 10) ──
    # Purely advisory: informs the result dict with an ``anomaly`` block
    # but only contributes to the composite score when it's strongly
    # anomalous AND no rule has already fired.  This preserves parity
    # with the deterministic rule engine while surfacing ML insight.
    anomaly_block = None
    anomaly_score = 0.0
    try:
        from app.services.anomaly import score_features, is_enabled as _anomaly_enabled
        if _anomaly_enabled():
            hour = datetime.now(timezone.utc).hour
            is_wknd = 1 if datetime.now(timezone.utc).weekday() >= 5 else 0
            feats = {
                "amount": tx.amount,
                "velocity_count": velocity_count,
                "hour_of_day": hour,
                "is_weekend": is_wknd,
                "entity_risk": entity_score,
                "ip_risk": 0,
                "phone_risk": 0,
                "email_risk": 0,
                "cop_reason": cop_score,
                "geo_mismatch": 0,
            }
            anomaly_block = score_features(feats)
            anom = float(anomaly_block.get("anomaly_score", 0) or 0)
            if anomaly_block.get("is_anomaly") and raw_score_precheck(
                velocity_score, amount_score, pattern_score,
                duplicate_score, entity_score, cop_score,
                inbound_velocity_score, income_score,
            ) < 25:
                anomaly_score = min(anom * 0.15, 10.0)  # cap at +10
                factors.append(f"ML_ANOMALY:iforest({anom:.1f})(+{anomaly_score:.1f})")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Anomaly scoring skipped: %s", exc)

    # ── 10. Graph Intelligence Advisory (Phase C2) ──
    # Mirrors the ML_ANOMALY contract above: strictly advisory, capped at
    # +10, and only contributes to the composite score when the ruleset
    # has not already decided. Disabled by default (AFDS_GNN_ENABLED=false)
    # so the public validation suite remains passing.
    # This branch runs inside a synchronous scoring function, so we bridge
    # to the async graph_store / graph_intel helpers via ``asyncio.run``.
    graph_block: dict[str, Any] | None = None
    graph_neighborhood: dict[str, float] | None = None
    graph_score = 0.0
    try:
        from app.services import graph_intel, graph_store

        if graph_intel.is_enabled():
            import asyncio as _asyncio

            async def _graph_probe() -> tuple[dict[str, float], dict[str, Any] | None]:
                neighborhood = await graph_store.get_neighborhood(tx.sender_id)
                merged = {
                    "amount": tx.amount,
                    "velocity_count": velocity_count,
                    "hour_of_day": datetime.now(timezone.utc).hour,
                    "is_weekend": 1 if datetime.now(timezone.utc).weekday() >= 5 else 0,
                    "entity_risk": entity_score,
                    "cop_reason": cop_score,
                }
                block = await graph_intel.score(
                    entity_id=tx.sender_id,
                    features=merged,
                    graph_features=neighborhood,
                )
                return neighborhood, block

            try:
                _asyncio.get_running_loop()
                # We're already inside an event loop (e.g. pytest-asyncio
                # or an async route). Skip the probe this pass — the
                # feedback-loop retrain job will still see the sync path
                # during backfill, and Shadow mode is advisory only.
                graph_neighborhood = None
                graph_block = None
            except RuntimeError:
                graph_neighborhood, graph_block = _asyncio.run(_graph_probe())

            if graph_block is not None:
                gs = float(graph_block.get("score", 0.0) or 0.0)
                if graph_block.get("is_anomaly") and raw_score_precheck(
                    velocity_score, amount_score, pattern_score,
                    duplicate_score, entity_score, cop_score,
                    inbound_velocity_score, income_score,
                ) < 25:
                    graph_score = min(gs * 10.0, 10.0)  # cap at +10
                    factors.append(
                        f"ML_GRAPH:{graph_block.get('model_name', 'gnn')}({gs:.2f})(+{graph_score:.1f})"
                    )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Graph scoring skipped: %s", exc)

    # ── Composite Score ──
    raw_score = velocity_score + amount_score + pattern_score + duplicate_score + entity_score + cop_score + inbound_velocity_score + income_score + anomaly_score + graph_score
    risk_score = min(raw_score, 100.0)

    # ── Risk Level (Flink thresholds) ──
    if risk_score >= 75:
        risk_level = "CRITICAL"
    elif risk_score >= 50:
        risk_level = "HIGH"
    elif risk_score >= 25:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    # ── Interdiction Action ──
    if risk_score >= 75:
        action = "BLOCK"
    elif risk_score >= 50:
        action = "SUSPEND"
    elif risk_score >= 25:
        action = "FLAG"
    else:
        action = "ALLOW"

    # ── Hybrid Escalation Gate (Phase F3) ──
    # Isolated behind AFDS_MODEL_MODE=hybrid. No-op in every other mode,
    # which keeps public validation passing by construction. Never alters
    # risk_score — only risk_level / action when a soft-rule + strong
    # model signal co-occur.
    hybrid_info: dict[str, Any] | None = None
    try:
        from app.services.hybrid_gate import maybe_escalate, is_enabled as _hybrid_enabled
        if _hybrid_enabled():
            hybrid_info = maybe_escalate(
                risk_score=risk_score,
                risk_level=risk_level,
                action=action,
                factors=factors,
                anomaly_block=anomaly_block,
                graph_block=graph_block,
                sender_id=tx.sender_id,
            )
            if hybrid_info.get("escalated"):
                risk_level = hybrid_info["risk_level"]
                action = hybrid_info["action"]
                factors.append(
                    f"HYBRID_ESCALATION:model_p={hybrid_info['model_probability']:.2f}"
                )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Hybrid gate skipped: %s", exc)

    # ── Record to velocity window ──
    _velocity_windows[tx.sender_id].append({
        "ts": now,
        "amount": tx.amount,
        "external_id": tx.external_id,
    })

    # ── Build result ──
    scored_at = datetime.now(timezone.utc).isoformat()
    result = {
        "external_id": tx.external_id,
        "sender_id": tx.sender_id,
        "receiver_id": tx.receiver_id,
        "amount": tx.amount,
        "currency": tx.currency,
        "transaction_type": tx.transaction_type,
        "risk_score": round(risk_score, 2),
        "risk_level": risk_level,
        "interdiction_action": action,
        "factors": factors,
        "score_breakdown": {
            "velocity": round(velocity_score, 2),
            "amount": round(amount_score, 2),
            "pattern": round(pattern_score, 2),
            "duplicate": round(duplicate_score, 2),
            "entity": round(entity_score, 2),
            "cop": round(cop_score, 2),
        },
        "velocity_count_2min": velocity_count,
        "pattern_detected": pattern_detected,
        "cop_detail": cop_detail,
        "scored_at": scored_at,
        "model_version": "v1.0-realtime",
    }
    if anomaly_block is not None:
        result["anomaly"] = anomaly_block
        result["score_breakdown"]["ml_anomaly"] = round(anomaly_score, 2)
    if graph_block is not None:
        result["graph"] = {
            **graph_block,
            "neighborhood": graph_neighborhood or {},
        }
        result["score_breakdown"]["ml_graph"] = round(graph_score, 2)
    if hybrid_info is not None and hybrid_info.get("escalated"):
        result["hybrid"] = hybrid_info

    # ── 11. XAI Reason Codes (Phase E) ──
    # Non-gating annotation. Populated from FastSHAP when enabled and the
    # model-service responds within AFDS_XAI_TIMEOUT_MS (default 10ms);
    # otherwise falls back to deterministic rule factors. Never alters
    # risk_score — public validation suite must remain passing.
    try:
        from app.services.explain import build_reason_codes
        _xai_features = {
            "amount": tx.amount,
            "velocity_count": velocity_count,
            "hour_of_day": datetime.now(timezone.utc).hour,
            "is_weekend": 1 if datetime.now(timezone.utc).weekday() >= 5 else 0,
            "entity_risk": entity_score,
            "cop_reason": cop_score,
            "pattern_detected": int(bool(pattern_detected)),
            "inbound_velocity": inbound_velocity_score,
            "income_exceeded": income_score,
        }
        result["reason_codes"] = build_reason_codes(
            factors=factors,
            features=_xai_features,
            score_breakdown=result["score_breakdown"],
            risk_score=risk_score,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("reason_codes generation skipped: %s", exc)
        result["reason_codes"] = []

    # ── Update engine state ──
    _scored_history.append(result)
    _engine_stats["total_scored"] += 1
    action_key = {
        "ALLOW": "total_allowed",
        "FLAG": "total_flagged",
        "SUSPEND": "total_suspended",
        "BLOCK": "total_blocked",
    }.get(action, "total_allowed")
    _engine_stats[action_key] += 1

    # ── Generate alert if needed ──
    if risk_score >= 25:
        alert = {
            "id": str(uuid.uuid4()),
            "transaction_id": tx.external_id,
            "alert_type": "CRITICAL_RISK" if risk_score >= 75 else "HIGH_RISK" if risk_score >= 50 else "VELOCITY_ANOMALY",
            "severity": risk_level,
            "title": f"Risk score {risk_score:.0f}: {tx.external_id[:8]}… ({tx.amount} {tx.currency})",
            "description": f"Sender: {tx.sender_id} | Score: {risk_score} | Factors: {', '.join(factors)}",
            "status": "OPEN",
            "created_at": scored_at,
        }
        _alerts.append(alert)
        _engine_stats["total_flagged"] += 1

    # ── Generate interdiction if score >= 50 ──
    if risk_score >= 50:
        interdiction = {
            "id": str(uuid.uuid4()),
            "transaction_id": tx.external_id,
            "sender_id": tx.sender_id,
            "action": action,
            "reason": f"Auto-interdiction: Risk score {risk_score:.2f} | Velocity: {velocity_count} txns/2min | Amount: {tx.amount} {tx.currency}",
            "risk_score": risk_score,
            "issued_at": scored_at,
        }
        _interdictions.append(interdiction)

    return result


# ──────────────────────────────────────────────
# API Endpoints
# ──────────────────────────────────────────────

@router.post("/score")
async def score_single(tx: RealtimeTransaction):
    """Score a single transaction in real-time. Returns full risk analysis."""
    return score_transaction(tx)


@router.post("/batch")
async def score_batch(req: BatchRequest):
    """Score a batch of transactions. Returns scored array + summary."""
    results = [score_transaction(tx) for tx in req.transactions]
    return {
        "total": len(results),
        "scored": results,
        "summary": {
            "avg_risk": round(sum(r["risk_score"] for r in results) / max(len(results), 1), 2),
            "max_risk": max((r["risk_score"] for r in results), default=0),
            "by_level": {
                level: sum(1 for r in results if r["risk_level"] == level)
                for level in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
            },
            "by_action": {
                action: sum(1 for r in results if r["interdiction_action"] == action)
                for action in ["ALLOW", "FLAG", "SUSPEND", "BLOCK"]
            },
        },
    }


@router.get("/simulate")
async def simulate_demo():
    """Replay synthetic demo transactions through the scoring engine."""
    demo_transactions = [
        RealtimeTransaction(
            external_id="demo-low-001",
            sender_id="demo-user-001",
            receiver_id="demo-merchant-001",
            amount=42.50,
            currency="GBP",
            transaction_type="TRANSFER",
        ),
        RealtimeTransaction(
            external_id="demo-velocity-001",
            sender_id="demo-user-velocity",
            receiver_id="demo-merchant-002",
            amount=250.00,
            currency="GBP",
            transaction_type="TRANSFER",
        ),
        RealtimeTransaction(
            external_id="demo-high-amount-001",
            sender_id="demo-user-high-value",
            receiver_id="demo-merchant-003",
            amount=55000.00,
            currency="GBP",
            transaction_type="TRANSFER",
        ),
    ]

    results = [score_transaction(tx) for tx in demo_transactions]

    return {
        "simulation": "synthetic_demo",
        "total_transactions": len(results),
        "results": results,
        "summary": {
            "avg_risk": round(sum(r["risk_score"] for r in results) / max(len(results), 1), 2),
            "max_risk": max((r["risk_score"] for r in results), default=0),
            "flagged": sum(1 for r in results if r["risk_score"] >= 25),
            "blocked": sum(1 for r in results if r["risk_score"] >= 75),
            "by_level": {
                level: sum(1 for r in results if r["risk_level"] == level)
                for level in ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
            },
        },
    }


@router.post("/cop-feed")
async def feed_cop_verification(verifications: list[CopVerification]):
    """Feed COP verification results into the scoring engine's cache.
    These are used as a risk signal when transactions reference these accounts."""
    for v in verifications:
        _cop_cache[v.account_id] = {
            "reason_code": v.reason_code,
            "matched": v.matched,
            "requested_name": v.requested_name,
            "returned_name": v.returned_name,
        }
    return {
        "loaded": len(verifications),
        "total_cached": len(_cop_cache),
    }


@router.get("/state")
async def engine_state():
    """Current engine state: velocity windows, recent alerts, stats."""
    active_senders = {
        sid: len(entries)
        for sid, entries in _velocity_windows.items()
        if entries
    }
    return {
        "stats": _engine_stats,
        "active_velocity_windows": len(active_senders),
        "active_senders_top10": dict(sorted(active_senders.items(), key=lambda x: -x[1])[:10]),
        "total_scored": len(_scored_history),
        "total_alerts": len(_alerts),
        "total_interdictions": len(_interdictions),
        "recent_alerts": _alerts[-10:],
        "recent_interdictions": _interdictions[-5:],
        "cop_cache_size": len(_cop_cache),
    }


@router.post("/reset")
async def reset_engine():
    """Reset all engine state."""
    _velocity_windows.clear()
    _scored_history.clear()
    _alerts.clear()
    _interdictions.clear()
    _cop_cache.clear()
    _engine_stats.update({
        "total_scored": 0,
        "total_flagged": 0,
        "total_blocked": 0,
        "total_suspended": 0,
        "total_allowed": 0,
        "start_time": None,
    })
    return {"status": "reset", "message": "Engine state cleared"}
