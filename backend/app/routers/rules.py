"""
Dynamic Rules Engine — Postgres-backed CRUD + Kafka publishing.

Storage: PostgreSQL (<configured_schema>.detection_rules) — replaces previous in-memory dict
which lost data on pod restart and diverged across replicas.

Flow: API/MCP → PostgreSQL + Kafka `detection-rules` → Flink applies in real-time
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from asyncio import wait_for, TimeoutError as AsyncTimeoutError
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import async_session, get_db
from app.core.kafka import get_kafka_producer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rules", tags=["rules"])

_SCHEMA_NAME = get_settings().postgres_schema or "afds"
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", _SCHEMA_NAME):
    raise RuntimeError(f"Invalid PostgreSQL schema name: {_SCHEMA_NAME!r}")
_RULES_TABLE = f"{_SCHEMA_NAME}.detection_rules"


# ── Schemas ──────────────────────────────────────────────────────────

class RuleCondition(BaseModel):
    field: str
    operator: str
    value: str


class RuleCreate(BaseModel):
    rule_name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    condition: RuleCondition
    action: str = Field(default="FLAG", pattern=r"^(BLOCK|SUSPEND|FLAG|ALLOW)$")
    risk_score_adjustment: int = Field(default=30, ge=0, le=100)
    severity: str = Field(default="HIGH", pattern=r"^(LOW|MEDIUM|HIGH|CRITICAL)$")


class RuleUpdate(BaseModel):
    description: str | None = None
    condition: RuleCondition | None = None
    action: str | None = Field(default=None, pattern=r"^(BLOCK|SUSPEND|FLAG|ALLOW)$")
    risk_score_adjustment: int | None = Field(default=None, ge=0, le=100)
    severity: str | None = Field(default=None, pattern=r"^(LOW|MEDIUM|HIGH|CRITICAL)$")
    active: bool | None = None


# ── Row helpers ──────────────────────────────────────────────────────

def _row_to_dict(row: Any) -> dict:
    """Map a SELECT * row from detection_rules to the JSON shape the UI expects."""
    cond = row.condition_json
    if isinstance(cond, str):
        try:
            cond = json.loads(cond)
        except Exception:
            cond = {}
    return {
        "id": str(row.id),
        "rule_name": row.rule_name,
        "description": row.description,
        "condition_json": cond,
        "action": row.action,
        "risk_score_adjustment": row.risk_score_adjustment,
        "severity": row.severity,
        "active": row.active,
        "created_by": row.created_by,
        "version": row.version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def _publish_kafka(payload: dict, key: str) -> None:
    try:
        producer = await wait_for(get_kafka_producer(), timeout=3.0)
        await wait_for(
            producer.send_and_wait(
                "detection-rules", key=key.encode(), value=json.dumps(payload).encode()
            ),
            timeout=3.0,
        )
    except (AsyncTimeoutError, Exception) as exc:
        logger.warning("Kafka publish failed (rule still persisted in Postgres): %s", exc)


# ── Seed-on-startup (idempotent) ────────────────────────────────────

_SAMPLE_RULES = [
    ("high_value_block", "Block transactions over £50,000",
     {"field": "amount", "operator": "gt", "value": "50000"},
     "BLOCK", 50, "CRITICAL", "system"),
    ("pep_sender_flag", "Flag transactions from PEP senders",
     {"field": "sender_pep", "operator": "is_true", "value": "true"},
     "FLAG", 35, "HIGH", "ai-mcp-agent"),
    ("kyc_none_suspend", "Suspend if sender KYC level is NONE",
     {"field": "sender_kyc_level", "operator": "eq", "value": "NONE"},
     "SUSPEND", 40, "HIGH", "ai-mcp-agent"),
    ("sanctioned_country_block", "Block senders from IRN, SYR, PRK",
     {"field": "sender_country", "operator": "in", "value": "IRN,SYR,PRK"},
     "BLOCK", 50, "CRITICAL", "system"),
    ("velocity_risk_alert", "Flag if velocity score exceeds 30",
     {"field": "velocity_score", "operator": "gt", "value": "30"},
     "FLAG", 25, "MEDIUM", "ai-mcp-agent"),
    ("frozen_card_block", "Block if sender card status is FROZEN",
     {"field": "sender_card_status", "operator": "eq", "value": "FROZEN"},
     "BLOCK", 45, "HIGH", "system"),
    ("receiver_pep_review", "Review if receiver is PEP and amount > 25k",
     {"field": "receiver_pep + amount", "operator": "multi",
      "value": "receiver_pep is_true true AND amount gt 25000"},
     "SUSPEND", 40, "HIGH", "ai-mcp-agent"),
]


async def seed_default_rules() -> None:
    """Insert the 7 default rules if they do not already exist (by rule_name)."""
    try:
        async with async_session() as db:
            await _ensure_rules_table(db)
            for name, desc, cond, action, adj, sev, by in _SAMPLE_RULES:
                await db.execute(
                    text(
                        f"""
                        INSERT INTO {_RULES_TABLE}
                          (rule_name, description, condition_json, action,
                           risk_score_adjustment, severity, created_by)
                        VALUES
                          (:name, :desc, CAST(:cond AS JSONB), :action,
                           :adj, :sev, :by)
                        ON CONFLICT (rule_name) DO NOTHING
                        """
                    ),
                    {
                        "name": name, "desc": desc, "cond": json.dumps(cond),
                        "action": action, "adj": adj, "sev": sev, "by": by,
                    },
                )
            await db.commit()
        logger.info("Default detection rules seeded (idempotent)")
    except Exception as exc:
        logger.warning("Could not seed default rules (DB may be initialising): %s", exc)


async def _ensure_rules_table(db: AsyncSession) -> None:
    """Create the configured detection_rules table if migrations have not run yet."""
    await db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_RULES_TABLE} (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                rule_name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT,
                condition_json JSONB NOT NULL,
                action VARCHAR(30) NOT NULL CHECK (action IN ('BLOCK', 'SUSPEND', 'FLAG', 'ALLOW')),
                risk_score_adjustment INT NOT NULL DEFAULT 0 CHECK (risk_score_adjustment >= 0 AND risk_score_adjustment <= 100),
                severity VARCHAR(20) NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by VARCHAR(255) NOT NULL DEFAULT 'SYSTEM',
                version INT NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    await db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_detection_rules_active ON {_RULES_TABLE}(active)"))
    await db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_detection_rules_severity ON {_RULES_TABLE}(severity)"))
    await db.execute(text(f"CREATE INDEX IF NOT EXISTS idx_detection_rules_condition ON {_RULES_TABLE} USING GIN (condition_json)"))


# ── Helpers used by rule_chat ────────────────────────────────────────

async def fetch_rule(rule_id: str, db: AsyncSession | None = None) -> dict | None:
    """Load a rule by id from Postgres. Returns None if missing.

    If `db` is provided (e.g. injected via FastAPI Depends), it is used so test
    overrides take effect. Otherwise a fresh session is opened from the pool.
    """
    try:
        UUID(rule_id)
    except ValueError:
        return None
    sql = text(
        f"""
        SELECT id, rule_name, description, condition_json, action,
               risk_score_adjustment, severity, active, created_by,
               version, created_at, updated_at
        FROM {_RULES_TABLE} WHERE id = CAST(:id AS UUID)
        """
    )
    if db is not None:
        row = (await db.execute(sql, {"id": rule_id})).first()
    else:
        async with async_session() as new_db:
            row = (await new_db.execute(sql, {"id": rule_id})).first()
    return _row_to_dict(row) if row else None


async def apply_rule_updates(
    rule_id: str, updates: dict, db: AsyncSession | None = None
) -> dict | None:
    """Apply a free-form updates dict (keys: description, condition_json, action,
    risk_score_adjustment, severity, active) to a rule. Returns the updated row."""
    if not updates:
        return await fetch_rule(rule_id, db=db)
    try:
        UUID(rule_id)
    except ValueError:
        return None

    fields: list[str] = []
    params: dict[str, Any] = {"id": rule_id}
    if "description" in updates:
        fields.append("description = :description")
        params["description"] = updates["description"]
    if "condition_json" in updates:
        fields.append("condition_json = CAST(:condition_json AS JSONB)")
        params["condition_json"] = json.dumps(updates["condition_json"])
    if "action" in updates:
        fields.append("action = :action")
        params["action"] = updates["action"]
    if "risk_score_adjustment" in updates:
        fields.append("risk_score_adjustment = :adj")
        params["adj"] = updates["risk_score_adjustment"]
    if "severity" in updates:
        fields.append("severity = :severity")
        params["severity"] = updates["severity"]
    if "active" in updates:
        fields.append("active = :active")
        params["active"] = updates["active"]
    if not fields:
        return await fetch_rule(rule_id, db=db)

    fields.append("updated_at = NOW()")
    fields.append("version = version + 1")
    sql = f"""
        UPDATE {_RULES_TABLE} SET {", ".join(fields)}
        WHERE id = CAST(:id AS UUID)
        RETURNING id, rule_name, description, condition_json, action,
                  risk_score_adjustment, severity, active, created_by,
                  version, created_at, updated_at
    """
    if db is not None:
        row = (await db.execute(text(sql), params)).first()
        if not row:
            return None
        await db.commit()
    else:
        async with async_session() as new_db:
            row = (await new_db.execute(text(sql), params)).first()
            if not row:
                await new_db.rollback()
                return None
            await new_db.commit()
    return _row_to_dict(row)


# ── Helper used by rule_chat.deploy ──────────────────────────────────

async def register_rule(
    rule_id: str,
    rule_name: str,
    description: str | None,
    conditions: list[dict],
    action: str,
    risk_score_adjustment: int,
    severity: str,
    created_by: str = "rule-chat",
    db: AsyncSession | None = None,
) -> dict:
    """Persist a rule built by the rule-chat flow."""
    if len(conditions) == 1:
        c = conditions[0]
        condition_json = {
            "field": c.get("field", ""),
            "operator": c.get("operator", ""),
            "value": str(c.get("value", "")),
        }
    else:
        condition_json = {
            "field": " + ".join(c.get("field", "") for c in conditions),
            "operator": "multi",
            "value": " AND ".join(
                f'{c.get("field","")} {c.get("operator","")} {c.get("value","")}'
                for c in conditions
            ),
        }

    insert_sql = text(
        f"""
        INSERT INTO {_RULES_TABLE}
          (id, rule_name, description, condition_json, action,
           risk_score_adjustment, severity, created_by)
        VALUES
          (CAST(:id AS UUID), :name, :desc, CAST(:cond AS JSONB), :action,
           :adj, :sev, :by)
        ON CONFLICT (rule_name) DO UPDATE SET
          description = EXCLUDED.description,
          condition_json = EXCLUDED.condition_json,
          action = EXCLUDED.action,
          risk_score_adjustment = EXCLUDED.risk_score_adjustment,
          severity = EXCLUDED.severity,
          updated_at = NOW(),
          version = {_RULES_TABLE}.version + 1
        RETURNING id, rule_name, description, condition_json, action,
                  risk_score_adjustment, severity, active, created_by,
                  version, created_at, updated_at
        """
    )
    params = {
        "id": rule_id, "name": rule_name, "desc": description,
        "cond": json.dumps(condition_json), "action": action,
        "adj": risk_score_adjustment, "sev": severity, "by": created_by,
    }
    if db is not None:
        row = (await db.execute(insert_sql, params)).first()
        await db.commit()
    else:
        async with async_session() as new_db:
            row = (await new_db.execute(insert_sql, params)).first()
            await new_db.commit()
    return _row_to_dict(row) if row else {
        "id": rule_id, "rule_name": rule_name, "description": description,
        "condition_json": condition_json, "action": action,
        "risk_score_adjustment": risk_score_adjustment, "severity": severity,
        "active": True, "created_by": created_by, "version": 1,
        "created_at": None, "updated_at": None,
    }


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/")
async def list_rules(active_only: bool = True, db: AsyncSession = Depends(get_db)):
    """List all detection rules from Postgres."""
    sql = f"""
        SELECT id, rule_name, description, condition_json, action,
               risk_score_adjustment, severity, active, created_by,
               version, created_at, updated_at
        FROM {_RULES_TABLE}
    """
    if active_only:
        sql += " WHERE active = TRUE"
    sql += " ORDER BY created_at DESC"
    rows = (await db.execute(text(sql))).all()
    rules = [_row_to_dict(r) for r in rows]
    return {"rules": rules, "count": len(rules)}


@router.post("/", status_code=201)
async def create_rule(body: RuleCreate, db: AsyncSession = Depends(get_db)):
    """Create a new detection rule and publish to Kafka for Flink consumption."""
    rule_id = str(uuid.uuid4())
    cond = body.condition.model_dump()

    try:
        result = await db.execute(
            text(
                f"""
                INSERT INTO {_RULES_TABLE}
                  (id, rule_name, description, condition_json, action,
                   risk_score_adjustment, severity, created_by)
                VALUES
                  (CAST(:id AS UUID), :name, :desc, CAST(:cond AS JSONB), :action,
                   :adj, :sev, 'api')
                RETURNING id, rule_name, description, condition_json, action,
                          risk_score_adjustment, severity, active, created_by,
                          version, created_at, updated_at
                """
            ),
            {
                "id": rule_id, "name": body.rule_name, "desc": body.description,
                "cond": json.dumps(cond), "action": body.action,
                "adj": body.risk_score_adjustment, "sev": body.severity,
            },
        )
        row = result.first()
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"Could not create rule: {exc}")

    rule = _row_to_dict(row)
    await _publish_kafka(
        {
            "rule_id": rule["id"], "rule_name": rule["rule_name"],
            "description": rule["description"], "condition": cond,
            "action": rule["action"], "risk_score_adjustment": rule["risk_score_adjustment"],
            "severity": rule["severity"], "active": True, "created_by": "api",
            "created_at": rule["created_at"], "version": rule["version"],
        },
        rule["id"],
    )
    return {"status": "created", "rule": rule}


@router.get("/{rule_id}")
async def get_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single rule by ID."""
    try:
        UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid rule id")
    row = (
        await db.execute(
            text(
                f"""
                SELECT id, rule_name, description, condition_json, action,
                       risk_score_adjustment, severity, active, created_by,
                       version, created_at, updated_at
                FROM {_RULES_TABLE} WHERE id = CAST(:id AS UUID)
                """
            ),
            {"id": rule_id},
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    return _row_to_dict(row)


@router.patch("/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate, db: AsyncSession = Depends(get_db)):
    """Update a rule and republish to Kafka."""
    try:
        UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid rule id")

    fields: list[str] = []
    params: dict[str, Any] = {"id": rule_id}
    if body.description is not None:
        fields.append("description = :description")
        params["description"] = body.description
    if body.condition is not None:
        fields.append("condition_json = CAST(:condition_json AS JSONB)")
        params["condition_json"] = json.dumps(body.condition.model_dump())
    if body.action is not None:
        fields.append("action = :action")
        params["action"] = body.action
    if body.risk_score_adjustment is not None:
        fields.append("risk_score_adjustment = :adj")
        params["adj"] = body.risk_score_adjustment
    if body.severity is not None:
        fields.append("severity = :severity")
        params["severity"] = body.severity
    if body.active is not None:
        fields.append("active = :active")
        params["active"] = body.active

    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields.append("updated_at = NOW()")
    fields.append("version = version + 1")

    sql = f"""
        UPDATE {_RULES_TABLE}
        SET {", ".join(fields)}
        WHERE id = CAST(:id AS UUID)
        RETURNING id, rule_name, description, condition_json, action,
                  risk_score_adjustment, severity, active, created_by,
                  version, created_at, updated_at
    """
    row = (await db.execute(text(sql), params)).first()
    if not row:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.commit()
    rule = _row_to_dict(row)

    await _publish_kafka(
        {
            "rule_id": rule["id"], "rule_name": rule["rule_name"],
            "condition": rule["condition_json"], "action": rule["action"],
            "risk_score_adjustment": rule["risk_score_adjustment"],
            "severity": rule["severity"], "active": rule["active"],
            "version": rule["version"], "updated_at": rule["updated_at"],
        },
        rule["id"],
    )
    return {"status": "updated", "rule": rule}


@router.delete("/{rule_id}")
async def deactivate_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    """Deactivate (soft-delete) a rule."""
    try:
        UUID(rule_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid rule id")

    row = (
        await db.execute(
            text(
                f"""
                UPDATE {_RULES_TABLE}
                SET active = FALSE, updated_at = NOW(), version = version + 1
                WHERE id = CAST(:id AS UUID)
                RETURNING id
                """
            ),
            {"id": rule_id},
        )
    ).first()
    if not row:
        await db.rollback()
        raise HTTPException(status_code=404, detail="Rule not found")
    await db.commit()

    deactivated_at = datetime.now(timezone.utc).isoformat()
    await _publish_kafka(
        {"rule_id": rule_id, "active": False, "deactivated_at": deactivated_at},
        rule_id,
    )
    return {"status": "deactivated", "rule_id": rule_id}
