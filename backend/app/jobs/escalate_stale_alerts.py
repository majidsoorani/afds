"""Stale alert escalation — CronJob runs every 30 minutes.

Escalates alerts that have exceeded SLA thresholds.
"""

import logging
import os
import sys
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# SLA thresholds (hours)
SLA_THRESHOLDS = {
    "CRITICAL": 1,
    "HIGH": 4,
    "MEDIUM": 24,
    "LOW": 72,
}


def get_db_url():
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "afds")
    user = os.getenv("POSTGRES_USER", "afds_admin")
    pw = os.getenv("POSTGRES_PASSWORD", "afds_secret")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _run_offline():
    """Demo mode when PostgreSQL is unavailable."""
    logger.info("Checking for stale alerts (OFFLINE MODE)...")

    import random, uuid
    from datetime import datetime, timezone, timedelta

    # Simulated open alerts with ages
    alerts = []
    for sev, sla_h in SLA_THRESHOLDS.items():
        for _ in range(random.randint(0, 3)):
            age_h = random.choice([sla_h * 0.5, sla_h * 1.2, sla_h * 2])
            alerts.append({
                "id": str(uuid.uuid4())[:8],
                "title": f"Demo {sev} alert",
                "severity": sev,
                "age_hours": age_h,
                "breached": age_h > sla_h,
            })

    escalated = 0
    for alert in alerts:
        if alert["breached"]:
            logger.warning(
                f"SLA BREACH: [{alert['severity']}] {alert['title']} "
                f"(age={alert['age_hours']:.1f}h, SLA={SLA_THRESHOLDS[alert['severity']]}h)"
            )
            escalated += 1

    logger.info(f"Escalation check complete. {escalated} alerts escalated.")


def run():
    logger.info("Checking for stale alerts...")
    try:
        conn = psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), running in OFFLINE mode")
        return _run_offline()

    try:
        cur = conn.cursor()
        escalated = 0

        for severity, hours in SLA_THRESHOLDS.items():
            cur.execute("""
                SELECT id, title, severity, status, created_at
                FROM afds.alerts
                WHERE status = 'OPEN'
                  AND severity = %s
                  AND created_at < NOW() - INTERVAL '%s hours'
            """, (severity, hours))
            stale = cur.fetchall()

            for alert in stale:
                # Log escalation
                cur.execute("""
                    INSERT INTO afds.audit_log
                        (event_type, entity_type, entity_id, actor, action, details)
                    VALUES ('ESCALATION', 'alert', %s, 'escalation-cronjob', 'SLA_BREACH',
                            %s::jsonb)
                """, (alert["id"], psycopg2.extras.Json({
                    "severity": severity,
                    "sla_hours": hours,
                    "alert_title": alert["title"],
                    "age_hours": str(alert["created_at"]),
                })))
                escalated += 1

            if stale:
                logger.warning(f"{len(stale)} {severity} alerts breached {hours}h SLA")

        conn.commit()
        logger.info(f"Escalation check complete. {escalated} alerts escalated.")

    except Exception as e:
        logger.error(f"Escalation check failed: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
