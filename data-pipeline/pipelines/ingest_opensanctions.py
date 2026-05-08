"""
AFDS Data Pipeline: OpenSanctions Ingestion

Automated pipeline to download and ingest sanctions data from OpenSanctions,
OFAC SDN, and UN Security Council lists into PostgreSQL for entity resolution.

Uses the zavod library for OpenSanctions data processing.
Supports full-load and delta-ingestion modes.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# OpenSanctions bulk data endpoints
OPENSANCTIONS_DATASETS = {
    "opensanctions-default": "https://data.opensanctions.org/datasets/latest/default/entities.ftm.json",
    "ofac-sdn": "https://data.opensanctions.org/datasets/latest/us_ofac_sdn/entities.ftm.json",
    "un-sc-sanctions": "https://data.opensanctions.org/datasets/latest/un_sc_sanctions/entities.ftm.json",
}

DATA_DIR = Path(__file__).parent.parent / "sanctions-data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_db_connection(
    host: str = "localhost",
    port: int = 5432,
    dbname: str = "afds",
    user: str = "afds_admin",
    password: str = "afds_secret",
):
    return psycopg2.connect(host=host, port=port, dbname=dbname, user=user, password=password)


def download_dataset(dataset_name: str, url: str) -> Path:
    """Download a dataset file from OpenSanctions."""
    output_path = DATA_DIR / f"{dataset_name}.json"
    logger.info("Downloading %s from %s...", dataset_name, url)

    with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=65536):
                f.write(chunk)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("Downloaded %s (%.1f MB)", dataset_name, size_mb)
    return output_path


def parse_ftm_entities(file_path: Path):
    """Parse FTM (FollowTheMoney) JSON Lines format."""
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed JSON line")
                continue


def ingest_entities(conn, dataset_name: str, file_path: Path) -> int:
    """Ingest parsed entities into PostgreSQL sanctions schema."""
    cursor = conn.cursor()
    count = 0
    batch_entities = []
    batch_names = []
    batch_identifiers = []
    batch_addresses = []
    batch_size = 1000

    for entity in parse_ftm_entities(file_path):
        entity_id = entity.get("id")
        if not entity_id:
            continue

        schema_type = entity.get("schema", "Unknown")
        caption = entity.get("caption", "")
        properties = entity.get("properties", {})
        datasets = entity.get("datasets", [dataset_name])

        batch_entities.append((
            entity_id, schema_type, caption,
            datetime.now(timezone.utc), datetime.now(timezone.utc), datetime.now(timezone.utc),
            datasets, json.dumps(properties),
            datetime.now(timezone.utc), datetime.now(timezone.utc),
        ))

        # Extract names
        for name in properties.get("name", []):
            batch_names.append((entity_id, name, name.lower().strip(), "primary", None))
        for alias in properties.get("alias", []):
            batch_names.append((entity_id, alias, alias.lower().strip(), "alias", None))

        # Extract identifiers
        for id_type in ["passportNumber", "idNumber", "taxNumber", "innCode"]:
            for val in properties.get(id_type, []):
                countries = properties.get("country", [None])
                batch_identifiers.append((entity_id, id_type, val, countries[0] if countries else None, None))

        # Extract addresses
        for addr in properties.get("address", []):
            countries = properties.get("country", [None])
            batch_addresses.append((entity_id, addr, countries[0] if countries else None, None, None))

        count += 1

        if len(batch_entities) >= batch_size:
            _flush_batch(cursor, batch_entities, batch_names, batch_identifiers, batch_addresses)
            batch_entities, batch_names, batch_identifiers, batch_addresses = [], [], [], []

    # Flush remaining
    if batch_entities:
        _flush_batch(cursor, batch_entities, batch_names, batch_identifiers, batch_addresses)

    conn.commit()
    cursor.close()
    return count


def _flush_batch(cursor, entities, names, identifiers, addresses):
    """Flush a batch of entities to the database."""
    if entities:
        execute_values(
            cursor,
            """INSERT INTO sanctions.entities 
               (id, schema_type, caption, first_seen, last_seen, last_change, datasets, properties, created_at, updated_at)
               VALUES %s
               ON CONFLICT (id) DO UPDATE SET
                   caption = EXCLUDED.caption,
                   last_seen = EXCLUDED.last_seen,
                   last_change = EXCLUDED.last_change,
                   datasets = EXCLUDED.datasets,
                   properties = EXCLUDED.properties,
                   updated_at = EXCLUDED.updated_at""",
            entities,
        )

    if names:
        execute_values(
            cursor,
            """INSERT INTO sanctions.entity_names 
               (entity_id, name, name_normalized, name_type, language)
               VALUES %s""",
            names,
        )

    if identifiers:
        execute_values(
            cursor,
            """INSERT INTO sanctions.entity_identifiers 
               (entity_id, identifier_type, identifier_value, country, authority)
               VALUES %s""",
            identifiers,
        )

    if addresses:
        execute_values(
            cursor,
            """INSERT INTO sanctions.entity_addresses 
               (entity_id, full_address, country, city, postal_code)
               VALUES %s""",
            addresses,
        )


def update_sync_status(conn, dataset_name: str, record_count: int, duration: int, status: str, error: str | None = None):
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE sanctions.sync_status 
           SET last_sync_at = NOW(), records_count = %s, sync_duration_seconds = %s, 
               status = %s, error_message = %s, updated_at = NOW()
           WHERE dataset_name = %s""",
        (record_count, duration, status, error, dataset_name),
    )
    conn.commit()
    cursor.close()


def run_full_ingestion(db_config: dict | None = None):
    """Run full ingestion of all sanctions datasets."""
    config = db_config or {}
    conn = get_db_connection(**config)

    for dataset_name, url in OPENSANCTIONS_DATASETS.items():
        start = time.time()
        try:
            # Clear existing names/identifiers for delta
            cursor = conn.cursor()
            cursor.execute("DELETE FROM sanctions.entity_names WHERE entity_id IN (SELECT id FROM sanctions.entities WHERE %s = ANY(datasets))", (dataset_name,))
            cursor.execute("DELETE FROM sanctions.entity_identifiers WHERE entity_id IN (SELECT id FROM sanctions.entities WHERE %s = ANY(datasets))", (dataset_name,))
            cursor.execute("DELETE FROM sanctions.entity_addresses WHERE entity_id IN (SELECT id FROM sanctions.entities WHERE %s = ANY(datasets))", (dataset_name,))
            conn.commit()
            cursor.close()

            file_path = download_dataset(dataset_name, url)
            count = ingest_entities(conn, dataset_name, file_path)
            duration = int(time.time() - start)

            update_sync_status(conn, dataset_name, count, duration, "SUCCESS")
            logger.info("Ingested %d entities from %s in %ds", count, dataset_name, duration)

        except Exception as e:
            duration = int(time.time() - start)
            update_sync_status(conn, dataset_name, 0, duration, "FAILED", str(e))
            logger.error("Failed to ingest %s: %s", dataset_name, e)

    conn.close()


if __name__ == "__main__":
    import os
    config = {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", 5432)),
        "dbname": os.environ.get("POSTGRES_DB", "afds"),
        "user": os.environ.get("POSTGRES_USER", "afds_admin"),
        "password": os.environ.get("POSTGRES_PASSWORD", "afds_secret"),
    }
    run_full_ingestion(config)
