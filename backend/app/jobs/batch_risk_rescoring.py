"""Batch risk re-scoring — CronJob runs daily at 3 AM.

Re-scores all transactions from the last 24h using latest detection rules.
"""

import json
import logging
import os
import sys
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def get_db_url():
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "afds")
    user = os.getenv("POSTGRES_USER", "afds_admin")
    pw = os.getenv("POSTGRES_PASSWORD", "afds_secret")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def evaluate_rule(tx: dict, rule: dict) -> bool:
    """Evaluate a single rule against a transaction."""
    condition = rule.get("condition_json", {})
    field = condition.get("field")
    operator = condition.get("operator")
    value = condition.get("value")

    if not field or not operator or value is None:
        return False

    tx_value = tx.get(field)
    if tx_value is None:
        return False

    try:
        if operator == "gt":
            return float(tx_value) > float(value)
        elif operator == "lt":
            return float(tx_value) < float(value)
        elif operator == "eq":
            return str(tx_value) == str(value)
        elif operator == "neq":
            return str(tx_value) != str(value)
        elif operator == "contains":
            return str(value).lower() in str(tx_value).lower()
        elif operator == "in":
            return str(tx_value) in [v.strip() for v in str(value).split(",")]
    except (ValueError, TypeError):
        return False

    return False


def _run_offline():
    """Demo mode when PostgreSQL is unavailable."""
    logger.info("Starting batch risk re-scoring (OFFLINE MODE)...")

    # Simulated rules
    rules = [
        {"rule_name": "high_value_block", "condition_json": {"field": "amount", "operator": "gt", "value": "50000"}, "risk_score_adjustment": 50, "action": "BLOCK"},
        {"rule_name": "velocity_flag", "condition_json": {"field": "amount", "operator": "gt", "value": "10000"}, "risk_score_adjustment": 20, "action": "FLAG"},
        {"rule_name": "pep_suspend", "condition_json": {"field": "sender_pep", "operator": "eq", "value": "true"}, "risk_score_adjustment": 40, "action": "SUSPEND"},
    ]
    logger.info(f"Loaded {len(rules)} active detection rules (demo)")

    # Simulated transactions
    import random, uuid
    transactions = []
    for i in range(50):
        transactions.append({
            "id": str(uuid.uuid4()),
            "amount": random.choice([100, 500, 5000, 15000, 75000, 120000]),
            "sender_id": f"user-{i:03d}",
            "current_score": random.randint(0, 30),
        })
    logger.info(f"Found {len(transactions)} transactions to re-score (demo)")

    rescored = 0
    for tx in transactions:
        total_adjustment = 0
        matched_rules = []
        for rule in rules:
            if evaluate_rule(tx, rule):
                total_adjustment += rule["risk_score_adjustment"]
                matched_rules.append(rule["rule_name"])
        if total_adjustment > 0:
            new_score = min(100, float(tx.get("current_score", 0) or 0) + total_adjustment)
            new_level = (
                "CRITICAL" if new_score >= 75 else "HIGH" if new_score >= 50
                else "MEDIUM" if new_score >= 25 else "LOW"
            )
            logger.info(f"  Re-scored {tx['id'][:12]}… amount={tx['amount']} → {new_score:.0f} ({new_level}) rules={matched_rules}")
            rescored += 1

    logger.info(f"Batch re-scoring complete. {rescored}/{len(transactions)} transactions re-scored.")


def run():
    logger.info("Starting batch risk re-scoring...")
    try:
        conn = psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), running in OFFLINE mode")
        return _run_offline()

    try:
        cur = conn.cursor()

        # Get active rules
        cur.execute("SELECT * FROM afds.detection_rules WHERE active = TRUE")
        rules = cur.fetchall()
        logger.info(f"Loaded {len(rules)} active detection rules")

        # Get transactions from last 24h
        cur.execute("""
            SELECT t.*, rs.risk_score AS current_score
            FROM afds.transactions t
            LEFT JOIN afds.risk_scores rs ON t.id = rs.transaction_id
            WHERE t.created_at > NOW() - INTERVAL '24 hours'
            ORDER BY t.created_at DESC
        """)
        transactions = cur.fetchall()
        logger.info(f"Found {len(transactions)} transactions to re-score")

        rescored = 0
        for tx in transactions:
            total_adjustment = 0
            matched_rules = []

            for rule in rules:
                if evaluate_rule(dict(tx), dict(rule)):
                    total_adjustment += rule["risk_score_adjustment"]
                    matched_rules.append(rule["rule_name"])

                    # Log rule execution
                    cur.execute("""
                        INSERT INTO afds.rule_executions
                            (rule_id, transaction_id, matched, risk_adjustment_applied, action_taken)
                        VALUES (%s, %s, TRUE, %s, %s)
                    """, (rule["id"], tx["id"], rule["risk_score_adjustment"], rule["action"]))

            if total_adjustment > 0:
                new_score = min(100, float(tx.get("current_score", 0) or 0) + total_adjustment)
                new_level = (
                    "CRITICAL" if new_score >= 75
                    else "HIGH" if new_score >= 50
                    else "MEDIUM" if new_score >= 25
                    else "LOW"
                )

                cur.execute("""
                    UPDATE afds.risk_scores
                    SET risk_score = %s, risk_level = %s,
                        factors = factors || %s::jsonb,
                        model_version = 'v1.0-batch-rescore'
                    WHERE transaction_id = %s
                """, (new_score, new_level,
                      json.dumps([{"rule": r, "source": "batch-rescore"} for r in matched_rules]),
                      tx["id"]))
                rescored += 1

        conn.commit()
        logger.info(f"Batch re-scoring complete. {rescored}/{len(transactions)} transactions re-scored.")

    except Exception as e:
        logger.error(f"Batch re-scoring failed: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
