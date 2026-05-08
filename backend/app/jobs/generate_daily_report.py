"""Daily compliance report generation — CronJob runs weekdays at 6 AM."""

import json
import logging
import os
import sys
from datetime import datetime, timezone
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


def _run_offline():
    """Demo mode when PostgreSQL is unavailable."""
    logger.info("Generating daily compliance report (OFFLINE MODE)...")

    report = {
        "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "OFFLINE_DEMO",
        "transactions": {
            "total": 1247, "blocked": 3, "suspended": 8,
            "total_volume": 2_345_678.90, "avg_amount": 1_880.65,
        },
        "risk_distribution": {"LOW": 1150, "MEDIUM": 62, "HIGH": 27, "CRITICAL": 8},
        "alerts": {"OPEN": 14, "INVESTIGATING": 6, "RESOLVED": 89, "DISMISSED": 23},
        "sar_filings": {"DRAFT": 2, "SUBMITTED": 1, "ACKNOWLEDGED": 5},
        "detection_rules": {"active": 12, "matches_24h": 47},
        "sanctions_screening": {"entities_screened": 312, "hits": 4},
    }

    logger.info(f"Daily report generated:\n{json.dumps(report, indent=2, default=str)}")
    return report


def run():
    logger.info("Generating daily compliance report...")
    try:
        conn = psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), running in OFFLINE mode")
        return _run_offline()

    try:
        cur = conn.cursor()

        # Transaction stats
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'BLOCKED') AS blocked,
                   COUNT(*) FILTER (WHERE status = 'SUSPENDED') AS suspended,
                   COALESCE(SUM(amount), 0) AS total_volume,
                   COALESCE(AVG(amount), 0) AS avg_amount
            FROM afds.transactions
            WHERE created_at > NOW() - INTERVAL '24 hours'
        """)
        tx_stats = dict(cur.fetchone())

        # Risk distribution
        cur.execute("""
            SELECT risk_level, COUNT(*) AS count
            FROM afds.risk_scores
            WHERE scored_at > NOW() - INTERVAL '24 hours'
            GROUP BY risk_level
        """)
        risk_dist = {row["risk_level"]: row["count"] for row in cur.fetchall()}

        # Alert stats
        cur.execute("""
            SELECT status, COUNT(*) AS count
            FROM afds.alerts
            GROUP BY status
        """)
        alert_stats = {row["status"]: row["count"] for row in cur.fetchall()}

        # SAR stats
        cur.execute("""
            SELECT status, COUNT(*) AS count
            FROM afds.sar_filings
            GROUP BY status
        """)
        sar_stats = {row["status"]: row["count"] for row in cur.fetchall()}

        # Rule stats
        cur.execute("SELECT COUNT(*) AS active FROM afds.detection_rules WHERE active = TRUE")
        rule_count = cur.fetchone()["active"]

        cur.execute("""
            SELECT COUNT(*) AS matches
            FROM afds.rule_executions
            WHERE created_at > NOW() - INTERVAL '24 hours'
        """)
        rule_matches = cur.fetchone()["matches"]

        report = {
            "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "transactions": {**tx_stats, "total_volume": float(tx_stats["total_volume"]), "avg_amount": float(tx_stats["avg_amount"])},
            "risk_distribution": risk_dist,
            "alerts": alert_stats,
            "sar_filings": sar_stats,
            "detection_rules": {"active": rule_count, "matches_24h": rule_matches},
        }

        # Save to audit log
        cur.execute("""
            INSERT INTO afds.audit_log
                (event_type, entity_type, entity_id, actor, action, details)
            VALUES ('REPORT', 'compliance_report', uuid_generate_v4(), 'report-cronjob', 'DAILY_REPORT', %s::jsonb)
        """, (json.dumps(report, default=str),))
        conn.commit()

        logger.info(f"Daily report generated: {json.dumps(report, indent=2, default=str)}")

    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
