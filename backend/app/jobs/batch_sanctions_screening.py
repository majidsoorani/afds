"""Batch sanctions re-screening — CronJob runs daily at 2 AM.

Re-screens all senders/receivers from the last 7 days against latest sanctions lists.
"""

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


def _run_offline():
    """Demo mode when PostgreSQL is unavailable."""
    logger.info("Starting batch sanctions re-screening (OFFLINE MODE)...")

    # Simulated entities from last 7 days
    entities = [
        "user-001", "user-002", "Viktor Bout", "Dawood Ibrahim",
        "John Smith", "Alice Johnson", "Kim Jong Un", "Normal User",
        "Banco Delta Asia", "user-010", "user-011", "user-012",
    ]
    logger.info(f"Found {len(entities)} unique entities to screen (demo)")

    # Offline sanctions names for matching
    sanctions_names = {
        "Viktor Bout": ("OFAC-SDN", 0.95),
        "Dawood Ibrahim": ("UN-SC", 0.92),
        "Kim Jong Un": ("OFAC-SDN", 0.98),
        "Banco Delta Asia": ("OFAC-SDN", 0.90),
    }

    hits = 0
    for entity in entities:
        for sname, (source, sim) in sanctions_names.items():
            if sname.lower() in entity.lower() or entity.lower() in sname.lower():
                hits += 1
                logger.warning(f"SANCTIONS HIT: {entity} → {sname} (similarity={sim:.2f}, source={source})")
                break

    logger.info(f"Batch screening complete. {hits}/{len(entities)} entities had sanctions hits.")


def run():
    logger.info("Starting batch sanctions re-screening...")
    try:
        conn = psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), running in OFFLINE mode")
        return _run_offline()

    try:
        cur = conn.cursor()

        # Get distinct entities from last 7 days
        cur.execute("""
            SELECT DISTINCT entity FROM (
                SELECT sender_id AS entity FROM afds.transactions WHERE created_at > NOW() - INTERVAL '7 days'
                UNION
                SELECT receiver_id AS entity FROM afds.transactions WHERE receiver_id IS NOT NULL AND created_at > NOW() - INTERVAL '7 days'
            ) entities
        """)
        entities = [row["entity"] for row in cur.fetchall()]
        logger.info(f"Found {len(entities)} unique entities to screen")

        hits = 0
        for entity in entities:
            cur.execute("SELECT * FROM sanctions.search_entity_names(%s, 0.5, 5)", (entity,))
            matches = cur.fetchall()
            if matches:
                hits += 1
                best = max(matches, key=lambda r: r.get("similarity", 0))
                logger.warning(
                    f"SANCTIONS HIT: {entity} → {best.get('matched_name')} "
                    f"(similarity={best.get('similarity', 0):.2f})"
                )
                # Insert screening result
                cur.execute("""
                    INSERT INTO sanctions.screening_results
                        (entity_name, matched_entity_id, similarity_score, screened_by)
                    VALUES (%s, %s, %s, 'batch-cronjob')
                    ON CONFLICT DO NOTHING
                """, (entity, best.get("entity_id"), best.get("similarity", 0)))

        conn.commit()
        logger.info(f"Batch screening complete. {hits}/{len(entities)} entities had sanctions hits.")

    except Exception as e:
        logger.error(f"Batch screening failed: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
