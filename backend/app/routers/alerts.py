"""Alert management endpoints for analyst dashboard."""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.models.schemas import AlertResponse, AlertUpdate

_s = get_settings().postgres_schema
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["Alerts"])


# ── Seed alerts for when DB is unavailable ───────────────────────────

def _seed_alerts() -> list[dict]:
    now = datetime.now(timezone.utc)
    samples = [
        {"type": "HIGH_VALUE_TRANSACTION", "severity": "CRITICAL", "status": "OPEN",
         "title": "£75,000 transfer blocked — sender PEP flagged",
         "desc": "user-alice-001 → user-bob-002, amount exceeds threshold + PEP match", "mins_ago": 12},
        {"type": "SANCTIONS_MATCH", "severity": "CRITICAL", "status": "OPEN",
         "title": "Sanctions match: receiver linked to SDN list",
         "desc": "user-quinn-017 partial name match (0.87) against OFAC SDN entity", "mins_ago": 45},
        {"type": "VELOCITY_SPIKE", "severity": "HIGH", "status": "OPEN",
         "title": "Velocity anomaly: 5 transfers in 8 minutes",
         "desc": "user-alice-001 rapid-fire sending pattern detected", "mins_ago": 60},
        {"type": "PATTERN_DETECTED", "severity": "HIGH", "status": "OPEN",
         "title": "Testing-the-waters pattern: escalating amounts",
         "desc": "user-eve-005 sent £500 → £2k → £12k → £48k in 4 hours", "mins_ago": 95},
        {"type": "KYC_RISK", "severity": "CRITICAL", "status": "OPEN",
         "title": "KYC NONE sender attempting £92,000 transfer",
         "desc": "user-kate-011 has no KYC verification, high-value blocked", "mins_ago": 110},
        {"type": "FROZEN_CARD", "severity": "HIGH", "status": "OPEN",
         "title": "Transaction attempt on frozen card",
         "desc": "user-tom-020 card status FROZEN, £28k SEND_MONEY suspended", "mins_ago": 168},
        {"type": "HIGH_VALUE_TRANSACTION", "severity": "MEDIUM", "status": "OPEN",
         "title": "£15,000 direct debit — elevated but within limits",
         "desc": "user-ivan-009 → user-julia-010, KYC STANDARD, flagged for review", "mins_ago": 180},
        {"type": "VELOCITY_SPIKE", "severity": "HIGH", "status": "OPEN",
         "title": "Cross-border velocity: 3 countries in 2 hours",
         "desc": "user-oscar-015 transactions from GBR, USA, CHE", "mins_ago": 200},
        {"type": "SANCTIONS_MATCH", "severity": "MEDIUM", "status": "RESOLVED",
         "title": "False positive: common name match cleared",
         "desc": "user-mike-013 name matched but DOB/nationality mismatch", "mins_ago": 240},
        {"type": "PATTERN_DETECTED", "severity": "LOW", "status": "RESOLVED",
         "title": "Round-amount pattern: 5 × £1,000 transactions",
         "desc": "user-rachel-018 regular salary payments confirmed", "mins_ago": 300},
    ]
    result = []
    for s in samples:
        result.append({
            "id": str(uuid.uuid4()),
            "transaction_id": str(uuid.uuid4()),
            "alert_type": s["type"],
            "severity": s["severity"],
            "title": s["title"],
            "description": s["desc"],
            "status": s["status"],
            "assigned_to": None,
            "created_at": (now - timedelta(minutes=s["mins_ago"])).isoformat(),
            "updated_at": (now - timedelta(minutes=s["mins_ago"])).isoformat(),
        })
    return result


_fallback_alerts = _seed_alerts()


@router.get("/")
async def list_alerts(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    severity: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List alerts for the analyst investigation queue."""
    try:
        conditions = []
        params: dict = {"limit": limit, "offset": offset}

        if status:
            conditions.append("status = :status")
            params["status"] = status
        if severity:
            conditions.append("severity = :severity")
            params["severity"] = severity

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM {_s}.alerts{where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"

        result = await db.execute(text(query), params)
        rows = result.fetchall()
        if rows:
            return rows
    except Exception as e:
        logger.warning(f"DB unavailable for alerts, using seed data: {e}")

    # Fallback seed data
    data = _fallback_alerts
    if status:
        data = [a for a in data if a["status"] == status]
    if severity:
        data = [a for a in data if a["severity"] == severity]
    return data[offset:offset + limit]


@router.get("/{alert_id}")
async def get_alert(alert_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single alert by ID."""
    try:
        result = await db.execute(
            text(f"SELECT * FROM {_s}.alerts WHERE id = :id"),
            {"id": alert_id},
        )
        row = result.fetchone()
        if row:
            return row
    except Exception as e:
        logger.warning(f"DB unavailable for alert lookup: {e}")

    for a in _fallback_alerts:
        if a["id"] == alert_id:
            return a
    raise HTTPException(status_code=404, detail="Alert not found")


@router.patch("/{alert_id}")
async def update_alert(
    alert_id: str,
    update: AlertUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update alert status (analyst action)."""
    try:
        result = await db.execute(
            text(
                f"UPDATE {_s}.alerts SET status = :status, updated_at = NOW() "
                "WHERE id = :id RETURNING *"
            ),
            {"id": alert_id, "status": update.status.value},
        )
        row = result.fetchone()
        if row:
            await db.commit()
            return row
    except Exception as e:
        logger.warning(f"DB unavailable for alert update: {e}")

    # Fallback: update in-memory
    for a in _fallback_alerts:
        if a["id"] == alert_id:
            a["status"] = update.status.value
            a["updated_at"] = datetime.now(timezone.utc).isoformat()
            return a
    raise HTTPException(status_code=404, detail="Alert not found")
