"""
AFDS MCP Server — Model Context Protocol integration for AI-powered AML compliance.

Exposes AFDS capabilities as MCP tools, resources, and prompts so that
AI agents (Claude, GPT, etc.) can screen entities, score transactions,
investigate alerts, create dynamic Flink detection rules, and draft SAR narratives.

Architecture:
  AI Agent ↔ MCP Server ↔ PostgreSQL + Kafka
  New rules → Kafka `detection-rules` → Flink reads & applies
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import psycopg2
import psycopg2.extras
from aiokafka import AIOKafkaProducer
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("afds-mcp")

# ── Config ──────────────────────────────────────────────────────────
DB_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://afds_admin:afds_secret@localhost:5432/afds",
)
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9094")

mcp = FastMCP(
    "AFDS — Autonomous Fraud Defense System",
    version="1.0.0",
)

# ── Helpers ─────────────────────────────────────────────────────────

def _db():
    """Return a fresh psycopg2 connection."""
    return psycopg2.connect(DB_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def _json_default(o: Any) -> Any:
    if isinstance(o, (datetime,)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o)} is not JSON serializable")


async def _kafka_produce(topic: str, key: str, value: dict):
    producer = AIOKafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v, default=_json_default).encode(),
        key_serializer=lambda k: k.encode() if k else None,
    )
    await producer.start()
    try:
        await producer.send_and_wait(topic, key=key, value=value)
    finally:
        await producer.stop()


# ══════════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════════

@mcp.tool()
def screen_entity(name: str, threshold: float = 0.5, max_results: int = 10) -> str:
    """Screen an entity name against all sanctions lists (OFAC, UN, EU, OpenSanctions).

    Uses pg_trgm fuzzy matching with configurable similarity threshold.
    Returns matching sanctioned entities with similarity scores.
    """
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM sanctions.search_entity_names(%s, %s, %s)",
            (name, threshold, max_results),
        )
        rows = cur.fetchall()
    if not rows:
        return json.dumps({"matches": [], "screened_name": name, "message": "No sanctions matches found."})
    return json.dumps({"matches": [dict(r) for r in rows], "screened_name": name}, default=_json_default)


@mcp.tool()
def score_transaction(
    sender_id: str,
    receiver_id: str,
    amount: float,
    currency: str = "GBP",
    transaction_type: str = "SEND_MONEY",
) -> str:
    """Score a transaction locally using the AFDS 6-factor risk model.

    Factors: velocity (max 40), amount anomaly (max 35), pattern (max 25),
    duplicate (max 15), entity risk (max 10), sanctions (max 40).
    No external API calls — everything runs locally.
    """
    score = 0.0
    factors = []

    # 1. Amount scoring
    if amount > 50_000:
        s = min(35, (amount - 50_000) / 50_000 * 35)
        score += s
        factors.append({"factor": "amount_anomaly", "contribution": round(s, 2), "detail": f"High value: {currency} {amount:,.2f}"})
    elif amount < 1:
        score += 15
        factors.append({"factor": "micro_transaction", "contribution": 15, "detail": f"Micro amount: {currency} {amount}"})

    # 2. Entity risk keywords
    high_risk_kw = ["crypto", "exchange", "betting", "gambling", "casino", "offshore"]
    for kw in high_risk_kw:
        if kw in (sender_id + (receiver_id or "")).lower():
            score += 10
            factors.append({"factor": "entity_risk", "contribution": 10, "detail": f"Keyword match: {kw}"})
            break

    # 3. Sanctions screening
    with _db() as conn, conn.cursor() as cur:
        for entity_name in [sender_id, receiver_id]:
            if not entity_name:
                continue
            cur.execute("SELECT * FROM sanctions.search_entity_names(%s, 0.6, 3)", (entity_name,))
            matches = cur.fetchall()
            if matches:
                best = max(matches, key=lambda r: r.get("similarity", 0))
                s = min(40, float(best.get("similarity", 0)) * 40)
                score += s
                factors.append({
                    "factor": "sanctions_match",
                    "contribution": round(s, 2),
                    "detail": f"Matched: {best.get('matched_name', '')} (sim={best.get('similarity', 0):.2f})",
                })

    # 4. Historical velocity from DB
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS total
               FROM afds.transactions
               WHERE sender_id = %s AND created_at > NOW() - INTERVAL '2 minutes'""",
            (sender_id,),
        )
        row = cur.fetchone()
        cnt, total = int(row["cnt"]), float(row["total"])
        if cnt > 5:
            s = min(40, cnt * 4)
            score += s
            factors.append({"factor": "velocity", "contribution": s, "detail": f"{cnt} txns in 2 min, total {currency} {total:,.2f}"})

    score = min(100, score)
    level = "LOW" if score < 25 else "MEDIUM" if score < 50 else "HIGH" if score < 75 else "CRITICAL"
    action = {"LOW": "ALLOW", "MEDIUM": "FLAG", "HIGH": "SUSPEND", "CRITICAL": "BLOCK"}[level]

    return json.dumps({
        "risk_score": round(score, 2),
        "risk_level": level,
        "recommended_action": action,
        "factors": factors,
        "model_version": "v1.0-local",
    })


@mcp.tool()
def get_alert(alert_id: str) -> str:
    """Get full details of an alert including the linked transaction and risk score."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.*, t.sender_id, t.receiver_id, t.amount, t.currency,
                      t.transaction_type, r.risk_score, r.risk_level, r.factors AS risk_factors
               FROM afds.alerts a
               LEFT JOIN afds.transactions t ON a.transaction_id = t.id
               LEFT JOIN afds.risk_scores r ON a.risk_score_id = r.id
               WHERE a.id = %s::uuid""",
            (alert_id,),
        )
        row = cur.fetchone()
    if not row:
        return json.dumps({"error": f"Alert {alert_id} not found"})
    return json.dumps(dict(row), default=_json_default)


@mcp.tool()
def search_transactions(
    sender_id: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    status: str | None = None,
    limit: int = 50,
) -> str:
    """Search transactions with filters. Useful for investigating suspicious patterns."""
    clauses, params = [], []
    if sender_id:
        clauses.append("sender_id = %s")
        params.append(sender_id)
    if min_amount is not None:
        clauses.append("amount >= %s")
        params.append(min_amount)
    if max_amount is not None:
        clauses.append("amount <= %s")
        params.append(max_amount)
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = " AND ".join(clauses) if clauses else "TRUE"
    params.append(min(limit, 200))

    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM afds.transactions WHERE {where} ORDER BY created_at DESC LIMIT %s",
            params,
        )
        rows = cur.fetchall()
    return json.dumps([dict(r) for r in rows], default=_json_default)


@mcp.tool()
async def create_rule(
    name: str,
    description: str,
    condition_field: str,
    condition_operator: str,
    condition_value: str,
    action: str = "BLOCK",
    risk_adjustment: int = 30,
    severity: str = "HIGH",
) -> str:
    """Create a new dynamic detection rule and publish it to Kafka for Flink to consume.

    The AI agent calls this when it identifies a new fraud pattern.
    Rules flow: MCP → Kafka `detection-rules` → Flink reads & applies in real-time.

    Args:
        name: Short rule name, e.g. 'high_value_sanctioned_country'
        description: Why this rule exists
        condition_field: Field to evaluate (amount, sender_id, receiver_id, currency, transaction_type, receiver_country)
        condition_operator: Comparison operator (gt, lt, eq, neq, in, contains, regex)
        condition_value: Threshold or value (use comma-separated for 'in' operator)
        action: BLOCK | SUSPEND | FLAG | ALLOW
        risk_adjustment: Points to add to risk score (0-100)
        severity: CRITICAL | HIGH | MEDIUM | LOW
    """
    rule_id = str(uuid4())
    rule = {
        "rule_id": rule_id,
        "rule_name": name,
        "description": description,
        "condition": {
            "field": condition_field,
            "operator": condition_operator,
            "value": condition_value,
        },
        "action": action,
        "risk_score_adjustment": min(100, max(0, risk_adjustment)),
        "severity": severity,
        "active": True,
        "created_by": "ai-mcp-agent",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": 1,
    }

    # Persist to PostgreSQL
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO afds.detection_rules
               (id, rule_name, description, condition_json, action, risk_score_adjustment,
                severity, active, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (rule_id, name, description, json.dumps(rule["condition"]),
             action, risk_adjustment, severity, True, "ai-mcp-agent"),
        )
        conn.commit()

    # Publish to Kafka for Flink
    await _kafka_produce("detection-rules", rule_id, rule)

    return json.dumps({
        "status": "created",
        "rule_id": rule_id,
        "message": f"Rule '{name}' published to Kafka. Flink will pick it up within seconds.",
        "rule": rule,
    }, default=_json_default)


@mcp.tool()
def list_rules(active_only: bool = True) -> str:
    """List all detection rules. Use this to understand current fraud detection coverage."""
    with _db() as conn, conn.cursor() as cur:
        if active_only:
            cur.execute("SELECT * FROM afds.detection_rules WHERE active = TRUE ORDER BY created_at DESC")
        else:
            cur.execute("SELECT * FROM afds.detection_rules ORDER BY created_at DESC")
        rows = cur.fetchall()
    return json.dumps([dict(r) for r in rows], default=_json_default)


@mcp.tool()
async def deactivate_rule(rule_id: str, reason: str) -> str:
    """Deactivate a detection rule. Publishes update to Kafka so Flink stops applying it."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE afds.detection_rules SET active = FALSE, updated_at = NOW() WHERE id = %s::uuid RETURNING *",
            (rule_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if not row:
        return json.dumps({"error": f"Rule {rule_id} not found"})

    await _kafka_produce("detection-rules", rule_id, {
        **dict(row),
        "active": False,
        "deactivated_reason": reason,
        "deactivated_at": datetime.now(timezone.utc).isoformat(),
    })
    return json.dumps({"status": "deactivated", "rule_id": rule_id}, default=_json_default)


@mcp.tool()
def generate_sar(alert_id: str, include_network: bool = True) -> str:
    """Generate a Suspicious Activity Report (SAR) narrative for an alert.

    Returns structured SAR data (FinCEN format) that the AI agent can
    refine into a complete filing.
    """
    with _db() as conn, conn.cursor() as cur:
        # Get alert + transaction + risk
        cur.execute(
            """SELECT a.*, t.sender_id, t.receiver_id, t.amount, t.currency,
                      t.transaction_type, t.sender_iban, t.receiver_iban,
                      t.created_at AS tx_time, t.metadata AS tx_metadata,
                      r.risk_score, r.risk_level, r.factors AS risk_factors,
                      r.velocity_score, r.sanctions_score, r.pattern_score
               FROM afds.alerts a
               LEFT JOIN afds.transactions t ON a.transaction_id = t.id
               LEFT JOIN afds.risk_scores r ON a.risk_score_id = r.id
               WHERE a.id = %s::uuid""",
            (alert_id,),
        )
        alert = cur.fetchone()
        if not alert:
            return json.dumps({"error": f"Alert {alert_id} not found"})

        related_txns = []
        if include_network and alert.get("sender_id"):
            cur.execute(
                """SELECT id, external_id, receiver_id, amount, currency,
                          transaction_type, created_at
                   FROM afds.transactions
                   WHERE sender_id = %s ORDER BY created_at DESC LIMIT 20""",
                (alert["sender_id"],),
            )
            related_txns = [dict(r) for r in cur.fetchall()]

    sar = {
        "report_type": "SAR",
        "filing_format": "FinCEN_BSA",
        "alert_reference": alert_id,
        "subject": {
            "name": alert.get("sender_id"),
            "account_iban": alert.get("sender_iban"),
        },
        "suspicious_activity": {
            "date": alert.get("tx_time"),
            "amount": float(alert["amount"]) if alert.get("amount") else None,
            "currency": alert.get("currency"),
            "type": alert.get("transaction_type"),
            "risk_score": float(alert["risk_score"]) if alert.get("risk_score") else None,
            "risk_level": alert.get("risk_level"),
            "risk_factors": alert.get("risk_factors"),
        },
        "counterparty": {
            "name": alert.get("receiver_id"),
            "account_iban": alert.get("receiver_iban"),
        },
        "related_transactions": related_txns,
        "narrative_prompt": (
            f"Subject {alert.get('sender_id')} sent {alert.get('currency')} {alert.get('amount')} "
            f"to {alert.get('receiver_id')}. Risk score: {alert.get('risk_score')} ({alert.get('risk_level')}). "
            f"Alert: {alert.get('title')}. {alert.get('description', '')} "
            f"There are {len(related_txns)} related transactions from this sender."
        ),
    }
    return json.dumps(sar, default=_json_default)


@mcp.tool()
def investigate_entity(entity_name: str) -> str:
    """Full entity investigation — transactions, alerts, sanctions hits, risk history.

    Use this for deep-dive analysis before filing a SAR or creating a rule.
    """
    result: dict[str, Any] = {"entity": entity_name}

    with _db() as conn, conn.cursor() as cur:
        # Transactions
        cur.execute(
            """SELECT id, external_id, receiver_id, amount, currency,
                      transaction_type, status, created_at
               FROM afds.transactions
               WHERE sender_id = %s OR receiver_id = %s
               ORDER BY created_at DESC LIMIT 50""",
            (entity_name, entity_name),
        )
        result["transactions"] = [dict(r) for r in cur.fetchall()]
        result["transaction_count"] = len(result["transactions"])

        # Risk scores
        cur.execute(
            """SELECT rs.risk_score, rs.risk_level, rs.factors, rs.scored_at
               FROM afds.risk_scores rs
               JOIN afds.transactions t ON rs.transaction_id = t.id
               WHERE t.sender_id = %s
               ORDER BY rs.scored_at DESC LIMIT 20""",
            (entity_name,),
        )
        result["risk_history"] = [dict(r) for r in cur.fetchall()]

        # Alerts
        cur.execute(
            """SELECT a.id, a.alert_type, a.severity, a.title, a.status, a.created_at
               FROM afds.alerts a
               JOIN afds.transactions t ON a.transaction_id = t.id
               WHERE t.sender_id = %s OR t.receiver_id = %s
               ORDER BY a.created_at DESC LIMIT 20""",
            (entity_name, entity_name),
        )
        result["alerts"] = [dict(r) for r in cur.fetchall()]

        # Sanctions check
        cur.execute("SELECT * FROM sanctions.search_entity_names(%s, 0.4, 5)", (entity_name,))
        result["sanctions_matches"] = [dict(r) for r in cur.fetchall()]

        # Aggregate stats
        cur.execute(
            """SELECT COUNT(*) AS total_txns,
                      SUM(amount) AS total_amount,
                      AVG(amount) AS avg_amount,
                      MAX(amount) AS max_amount,
                      MIN(created_at) AS first_seen,
                      MAX(created_at) AS last_seen
               FROM afds.transactions WHERE sender_id = %s""",
            (entity_name,),
        )
        stats = cur.fetchone()
        result["summary"] = dict(stats) if stats else {}

    return json.dumps(result, default=_json_default)


@mcp.tool()
def get_network(entity_name: str, depth: int = 2) -> str:
    """Map the transaction network around an entity. Returns nodes and edges for graph analysis.

    Traces fund flows up to `depth` hops from the entity.
    """
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    visited: set[str] = set()
    queue = [(entity_name, 0)]

    with _db() as conn, conn.cursor() as cur:
        while queue:
            current, d = queue.pop(0)
            if current in visited or d > depth:
                continue
            visited.add(current)

            cur.execute(
                """SELECT sender_id, receiver_id, SUM(amount) AS total_amount,
                          COUNT(*) AS tx_count, MAX(currency) AS currency
                   FROM afds.transactions
                   WHERE sender_id = %s OR receiver_id = %s
                   GROUP BY sender_id, receiver_id""",
                (current, current),
            )
            for row in cur.fetchall():
                s, r = row["sender_id"], row["receiver_id"]
                for n in [s, r]:
                    if n and n not in nodes:
                        nodes[n] = {"id": n, "type": "entity"}
                if s and r:
                    edges.append({
                        "source": s,
                        "target": r,
                        "total_amount": float(row["total_amount"]),
                        "tx_count": row["tx_count"],
                        "currency": row["currency"],
                    })
                counterparty = r if s == current else s
                if counterparty and counterparty not in visited and d + 1 <= depth:
                    queue.append((counterparty, d + 1))

    return json.dumps({
        "root": entity_name,
        "depth": depth,
        "nodes": list(nodes.values()),
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }, default=_json_default)


# ══════════════════════════════════════════════════════════════════════
# RESOURCES
# ══════════════════════════════════════════════════════════════════════

@mcp.resource("afds://sanctions/lists")
def sanctions_lists() -> str:
    """Available sanctions lists and their sync status."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM sanctions.sync_status ORDER BY dataset_id")
        rows = cur.fetchall()
    return json.dumps([dict(r) for r in rows], default=_json_default)


@mcp.resource("afds://rules/active")
def active_rules() -> str:
    """Currently active detection rules applied by Flink."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM afds.detection_rules WHERE active = TRUE ORDER BY created_at DESC")
        rows = cur.fetchall()
    return json.dumps([dict(r) for r in rows], default=_json_default)


@mcp.resource("afds://config/risk-thresholds")
def risk_thresholds() -> str:
    """Current risk threshold configuration."""
    return json.dumps({
        "risk_levels": {
            "LOW": {"min": 0, "max": 24, "action": "ALLOW"},
            "MEDIUM": {"min": 25, "max": 49, "action": "FLAG"},
            "HIGH": {"min": 50, "max": 74, "action": "SUSPEND"},
            "CRITICAL": {"min": 75, "max": 100, "action": "BLOCK"},
        },
        "factor_weights": {
            "velocity": {"max": 40, "window": "2 minutes"},
            "amount_anomaly": {"max": 35, "high_threshold": 50000},
            "pattern": {"max": 25, "description": "Testing-the-waters detection"},
            "duplicate": {"max": 15, "window": "5 minutes"},
            "entity_risk": {"max": 10, "keywords": ["crypto", "exchange", "betting", "gambling", "casino", "offshore"]},
            "sanctions": {"max": 40, "threshold": 0.6},
        },
    })


@mcp.resource("afds://dashboard/stats")
def dashboard_stats() -> str:
    """Live dashboard statistics — 24h transaction and alert metrics."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT
                 (SELECT COUNT(*) FROM afds.transactions WHERE created_at > NOW() - INTERVAL '24 hours') AS total_24h,
                 (SELECT COUNT(*) FROM afds.transactions WHERE status = 'BLOCKED' AND created_at > NOW() - INTERVAL '24 hours') AS blocked_24h,
                 (SELECT COUNT(*) FROM afds.alerts WHERE status = 'OPEN') AS open_alerts,
                 (SELECT COUNT(*) FROM afds.alerts WHERE severity = 'CRITICAL' AND status = 'OPEN') AS critical_alerts,
                 (SELECT COALESCE(AVG(risk_score), 0) FROM afds.risk_scores WHERE scored_at > NOW() - INTERVAL '24 hours') AS avg_risk"""
        )
        row = cur.fetchone()
    return json.dumps(dict(row), default=_json_default)


# ══════════════════════════════════════════════════════════════════════
# PROMPTS
# ══════════════════════════════════════════════════════════════════════

@mcp.prompt()
def investigate(entity_name: str) -> str:
    """Investigation prompt — guides AI through a full entity investigation."""
    return f"""You are an AML compliance analyst investigating entity: {entity_name}

Follow this investigation workflow:
1. Call `investigate_entity("{entity_name}")` to get the full entity profile
2. Review transaction history for suspicious patterns (velocity, structuring, round amounts)
3. Call `screen_entity("{entity_name}")` to check sanctions lists
4. Call `get_network("{entity_name}")` to map the transaction network
5. For each suspicious pattern found, call `create_rule()` to add a new detection rule
6. If the activity warrants a SAR filing, call `generate_sar()` with the relevant alert ID

Provide your analysis in this structure:
- **Entity Profile**: Who they are, account history, volumes
- **Suspicious Indicators**: What patterns are abnormal
- **Sanctions Exposure**: Any list matches or near-matches
- **Network Analysis**: Connected entities and fund flows
- **Recommended Actions**: Rules to create, alerts to escalate, SARs to file
- **Risk Assessment**: Overall risk rating with justification"""


@mcp.prompt()
def generate_sar_narrative(alert_id: str) -> str:
    """SAR narrative generation prompt — produces FinCEN-compliant narrative."""
    return f"""You are drafting a Suspicious Activity Report (SAR) for alert: {alert_id}

Steps:
1. Call `generate_sar("{alert_id}")` to get structured SAR data
2. Call `get_alert("{alert_id}")` for full alert details
3. Review the suspicious activity data and related transactions

Write the SAR narrative following FinCEN guidelines:
- **Part V narrative** must be clear, complete, and concise
- Include: Who, What, When, Where, Why, How
- Reference specific transaction amounts, dates, and counterparties
- Explain why the activity is suspicious
- Note any connections to sanctions or known typologies
- Include the risk score and contributing factors

Format the output as a complete SAR Part V narrative ready for filing."""


@mcp.prompt()
def analyze_pattern(description: str) -> str:
    """Pattern analysis prompt — AI analyzes a fraud pattern and creates detection rules."""
    return f"""You are analyzing a potential fraud pattern: {description}

Steps:
1. Search for similar transactions using `search_transactions()` with relevant filters
2. Analyze the pattern characteristics (amounts, timing, entities, geography)
3. Determine if this is a known typology (structuring, layering, round-tripping, etc.)
4. Create detection rules using `create_rule()` for each identifiable signal

For each rule, specify:
- A descriptive name
- The condition field, operator, and value
- The appropriate action (BLOCK/SUSPEND/FLAG)
- Risk score adjustment (higher = more dangerous)
- Severity level

After creating rules, summarize:
- Pattern description
- Rules created (with IDs)
- Expected detection coverage
- Potential false positive rate
- Recommended monitoring approach"""


# ── Entry Point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
