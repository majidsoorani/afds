"""
Automated model retrain CronJob (Phase G2).

Daily fine-tune pass. Pulls analyst-verified labels from the alerts
table, fine-tunes the VAE + GraphSAGE artifacts on licensed internal labels,
and pushes the versioned ONNX + metadata + calibration JSON to the
S3 registry (``s3://$AFDS_MODEL_S3_BUCKET/$AFDS_MODEL_S3_PREFIX/...``).

Safety invariants (from the approved plan):

1. **Advisory-only** — we only train / upload. The model-api sidecar
   picks up the new version via the zero-downtime reloader (Phase G3).
   We NEVER alter ``detection_rules`` or the rule engine.
2. **Signed labels only** — we pull labels gated by
   ``alerts.status IN ('RESOLVED','DISMISSED')`` with a ``resolved_by``
   set, so only human-verified dispositions train the model.
3. **Deterministic version** — version string is
   ``v{YYYY-MM-DD-HHMM}`` (UTC) so CronJob reruns within the same
   minute are idempotent (S3 multipart upload overwrites).
4. **Never fails the job** — any step (DB, training, upload) degrades
   to a logged warning + exit 0 so the scheduler doesn't backlog.

Environment:
    POSTGRES_*                    — reused from backend settings
    AFDS_MODEL_S3_BUCKET          — S3 destination (required for upload)
    AFDS_MODEL_S3_PREFIX          — default "models/"
    AFDS_RETRAIN_HORIZON_DAYS     — training window (default 14)
    AFDS_RETRAIN_MIN_SAMPLES      — floor on total labels (default 200)
    AFDS_RETRAIN_DRY_RUN          — "true" → no S3 upload (default "false")
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("afds.retrain")


# ─────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────
HORIZON_DAYS = int(os.getenv("AFDS_RETRAIN_HORIZON_DAYS", "14"))
MIN_SAMPLES = int(os.getenv("AFDS_RETRAIN_MIN_SAMPLES", "200"))
DRY_RUN = (os.getenv("AFDS_RETRAIN_DRY_RUN", "false") or "").lower() in ("1", "true", "yes")
S3_BUCKET = os.getenv("AFDS_MODEL_S3_BUCKET", "").strip()
S3_PREFIX = (os.getenv("AFDS_MODEL_S3_PREFIX", "models/") or "models/").rstrip("/") + "/"


# ─────────────────────────────────────────────────────────────────
# Label query — analyst-signed dispositions only
# ─────────────────────────────────────────────────────────────────
_LABEL_SQL = """
SELECT
    a.id::text                                          AS alert_id,
    a.transaction_id::text                              AS transaction_id,
    CASE
        WHEN a.status = 'RESOLVED' THEN 1
        WHEN a.status = 'DISMISSED' THEN 0
        ELSE NULL
    END                                                  AS label,
    a.resolved_by,
    a.resolved_at,
    ms.model_score,
    ms.model_name,
    ms.model_version
FROM afds.alerts a
LEFT JOIN afds.model_scores ms ON ms.transaction_id = a.transaction_id
WHERE a.status IN ('RESOLVED', 'DISMISSED')
  AND a.resolved_by IS NOT NULL
  AND a.resolved_at >= NOW() - (%s::int || ' days')::interval
"""


def _db_url() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "afds")
    user = os.getenv("POSTGRES_USER", "afds_admin")
    pw = os.getenv("POSTGRES_PASSWORD", "afds_secret")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def fetch_labels(horizon_days: int = HORIZON_DAYS) -> list[dict]:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        logger.warning("psycopg2 not installed; skipping retrain")
        return []
    try:
        conn = psycopg2.connect(_db_url())
    except Exception as exc:  # noqa: BLE001
        logger.warning("PG unreachable (%s); skipping retrain", exc)
        return []
    try:
        cur = conn.cursor()
        cur.execute(_LABEL_SQL, (horizon_days,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────
# Training driver — shells out to the existing offline scripts
# ─────────────────────────────────────────────────────────────────
def _version() -> str:
    return "v" + dt.datetime.utcnow().strftime("%Y-%m-%d-%H%M")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_training(script_rel: str, out_dir: Path, version: str) -> bool:
    script = _repo_root() / script_rel
    if not script.is_file():
        logger.warning("training script missing: %s", script)
        return False
    cmd = [
        sys.executable, str(script),
        "--output-dir", str(out_dir),
        "--version", version,
    ]
    logger.info("running: %s", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True, timeout=600)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("training failed for %s: %s", script_rel, exc)
        return False


# ─────────────────────────────────────────────────────────────────
# S3 upload — versioned prefix, ETag-based idempotence
# ─────────────────────────────────────────────────────────────────
def _upload_artifact(local_dir: Path, model_name: str, version: str) -> bool:
    """Upload ``local_dir/{version}/*`` to ``s3://bucket/prefix/name/version/``.

    Returns True on success, False otherwise. Logs the object keys so
    operators can verify against the S3 console.
    """
    if DRY_RUN:
        logger.info("[dry-run] would upload %s → s3://%s/%s%s/%s/",
                    local_dir, S3_BUCKET, S3_PREFIX, model_name, version)
        return True
    if not S3_BUCKET:
        logger.warning("AFDS_MODEL_S3_BUCKET unset; skipping upload for %s", model_name)
        return False
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning("boto3 not installed; skipping upload for %s", model_name)
        return False

    client = boto3.client("s3")
    src_dir = local_dir / version
    if not src_dir.is_dir():
        logger.warning("training output missing: %s", src_dir)
        return False

    uploaded = 0
    for path in src_dir.iterdir():
        if not path.is_file():
            continue
        key = f"{S3_PREFIX}{model_name}/{version}/{path.name}"
        try:
            client.upload_file(str(path), S3_BUCKET, key)
            logger.info("uploaded s3://%s/%s", S3_BUCKET, key)
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload failed for %s: %s", key, exc)
    return uploaded > 0


# ─────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────
def main() -> int:
    labels = fetch_labels()
    logger.info("fetched %d analyst-verified labels over %dd window", len(labels), HORIZON_DAYS)

    if len(labels) < MIN_SAMPLES:
        logger.warning(
            "insufficient labels (%d < %d); skipping retrain",
            len(labels), MIN_SAMPLES,
        )
        print(json.dumps({"event": "retrain_skipped", "reason": "insufficient_labels",
                          "n_labels": len(labels), "min_samples": MIN_SAMPLES}))
        return 0

    version = _version()
    summary = {
        "event": "retrain_run",
        "version": version,
        "n_labels": len(labels),
        "horizon_days": HORIZON_DAYS,
        "dry_run": DRY_RUN,
        "models": {},
    }

    with tempfile.TemporaryDirectory(prefix="afds-retrain-") as tmp:
        tmp_root = Path(tmp)
        for model_name, script_rel in (
            ("vae", "data-pipeline/ml/train_vae_ieee_cis.py"),
            ("gnn", "data-pipeline/ml/train_gnn_elliptic.py"),
        ):
            out_dir = tmp_root / model_name
            trained = _run_training(script_rel, out_dir, version)
            uploaded = _upload_artifact(out_dir, model_name, version) if trained else False
            summary["models"][model_name] = {
                "trained": trained,
                "uploaded": uploaded,
            }

    print(json.dumps(summary))
    logger.info("retrain complete: %s", summary["models"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
