"""Sanctions list sync — CronJob runs daily at 1 AM.

Downloads latest sanctions data from OpenSanctions, OFAC, UN.
"""

import logging
import os
import sys
import httpx
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SOURCES = [
    {
        "dataset_id": "opensanctions-default",
        "url": "https://data.opensanctions.org/datasets/latest/default/entities.ftm.json",
        "name": "OpenSanctions Default",
    },
    {
        "dataset_id": "us-ofac-sdn",
        "url": "https://data.opensanctions.org/datasets/latest/us_ofac_sdn/entities.ftm.json",
        "name": "OFAC SDN",
    },
    {
        "dataset_id": "un-sc-sanctions",
        "url": "https://data.opensanctions.org/datasets/latest/un_sc_sanctions/entities.ftm.json",
        "name": "UN SC Sanctions",
    },
]


def get_db_url():
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "afds")
    user = os.getenv("POSTGRES_USER", "afds_admin")
    pw = os.getenv("POSTGRES_PASSWORD", "afds_secret")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def _run_offline():
    """Demo mode when PostgreSQL is unavailable — validate sources are reachable."""
    logger.info("Starting sanctions list sync (OFFLINE MODE)...")

    for source in SOURCES:
        try:
            logger.info(f"Checking {source['name']} ({source['url'][:60]}…)")
            resp = httpx.head(source["url"], timeout=15, follow_redirects=True)
            size = resp.headers.get("content-length", "unknown")
            logger.info(f"  ✓ {source['name']} reachable (status={resp.status_code}, size={size} bytes)")
        except Exception as e:
            logger.warning(f"  ✗ {source['name']} unreachable: {e}")

    logger.info("Sanctions list sync complete (offline — source validation only).")


def run():
    logger.info("Starting sanctions list sync...")
    try:
        conn = psycopg2.connect(get_db_url(), cursor_factory=psycopg2.extras.RealDictCursor)
    except Exception as e:
        logger.warning(f"PostgreSQL unavailable ({e}), running in OFFLINE mode")
        return _run_offline()

    for source in SOURCES:
        try:
            logger.info(f"Downloading {source['name']}...")
            cur = conn.cursor()

            # Update sync status
            cur.execute("""
                INSERT INTO sanctions.sync_status (dataset_id, status, started_at)
                VALUES (%s, 'syncing', NOW())
                ON CONFLICT (dataset_id)
                DO UPDATE SET status = 'syncing', started_at = NOW()
            """, (source["dataset_id"],))
            conn.commit()

            # Stream download and parse
            count = 0
            with httpx.stream("GET", source["url"], timeout=300) as response:
                response.raise_for_status()
                batch = []
                for line in response.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        import json
                        entity = json.loads(line)
                        if entity.get("schema") in ("Person", "Company", "Organization", "LegalEntity"):
                            names = entity.get("properties", {}).get("name", [])
                            for name in names:
                                batch.append((entity["id"], name, source["dataset_id"]))
                                count += 1

                            if len(batch) >= 1000:
                                psycopg2.extras.execute_values(
                                    cur,
                                    """INSERT INTO sanctions.entity_names
                                       (entity_id, name, dataset_id)
                                       VALUES %s
                                       ON CONFLICT DO NOTHING""",
                                    batch,
                                )
                                conn.commit()
                                batch = []
                    except Exception:
                        continue

                if batch:
                    psycopg2.extras.execute_values(
                        cur,
                        """INSERT INTO sanctions.entity_names
                           (entity_id, name, dataset_id)
                           VALUES %s
                           ON CONFLICT DO NOTHING""",
                        batch,
                    )
                    conn.commit()

            # Update sync status
            cur.execute("""
                UPDATE sanctions.sync_status
                SET status = 'completed', completed_at = NOW(), record_count = %s
                WHERE dataset_id = %s
            """, (count, source["dataset_id"]))
            conn.commit()
            logger.info(f"Synced {count} names from {source['name']}")

        except Exception as e:
            logger.error(f"Failed to sync {source['name']}: {e}")
            cur.execute("""
                UPDATE sanctions.sync_status
                SET status = 'failed', error_message = %s
                WHERE dataset_id = %s
            """, (str(e)[:500], source["dataset_id"]))
            conn.commit()

    conn.close()
    logger.info("Sanctions list sync complete.")


if __name__ == "__main__":
    run()
