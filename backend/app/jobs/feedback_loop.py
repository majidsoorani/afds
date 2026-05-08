"""Gap 9 — Nightly chargeback feedback loop CronJob.

Emits a JSON report of per-factor TP/FP precision over a trailing window
and logs 'tighten' / 'loosen' advisories. Advisory-only — never mutates
detection_rules. Falls back to a deterministic offline demo when PG is
unreachable, mirroring the pattern used by escalate_stale_alerts.
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HORIZON_DAYS = int(os.getenv("AFDS_FEEDBACK_HORIZON_DAYS", "30"))


def _db_url() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "afds")
    user = os.getenv("POSTGRES_USER", "afds_admin")
    pw = os.getenv("POSTGRES_PASSWORD", "afds_secret")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _log_report(report: dict) -> None:
    s = report["summary"]
    logger.info(
        "feedback-loop horizon=%dd source=%s tighten=%d loosen=%d hold=%d insufficient=%d",
        report["horizon_days"], report["source"],
        len(s["tighten"]), len(s["loosen"]), len(s["hold"]), len(s["insufficient_data"]),
    )
    for f in report["factors"]:
        if f["suggestion"] in ("tighten", "loosen"):
            logger.warning(
                "ADVISORY %-7s factor=%s precision=%s tp=%d fp=%d",
                f["suggestion"].upper(), f["factor"], f["precision"], f["tp"], f["fp"],
            )
    # machine-readable line for log-scraping
    print(json.dumps({"event": "feedback_report", **report}))


def run() -> None:
    from app.services.feedback_loop import compute_from_postgres, demo_report

    try:
        import psycopg2
        conn = psycopg2.connect(_db_url())
    except Exception as e:
        logger.warning("PostgreSQL unavailable (%s); emitting demo report", e)
        _log_report(demo_report(horizon_days=HORIZON_DAYS))
        return

    try:
        report = compute_from_postgres(conn, horizon_days=HORIZON_DAYS)
        _log_report(report)
    except Exception as e:
        logger.error("Feedback loop failed: %s", e)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
