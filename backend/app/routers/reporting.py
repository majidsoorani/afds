"""
Regulatory Reporting — SAR/STR auto-generation and filing management.

Supports FinCEN BSA (US), FCA (UK), BaFin (DE) formats.
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reporting", tags=["reporting"])


# ── Schemas ──────────────────────────────────────────────────────────

class SARCreateRequest(BaseModel):
    alert_id: str
    filing_format: str = Field(default="FinCEN_BSA", pattern=r"^(FinCEN_BSA|FCA_UK|BaFin_DE)$")
    narrative: str | None = None


class SARUpdateRequest(BaseModel):
    status: str = Field(pattern=r"^(DRAFT|PENDING_REVIEW|APPROVED|FILED|REJECTED)$")
    narrative: str | None = None
    filed_by: str | None = None


class SARResponse(BaseModel):
    id: str
    alert_id: str | None
    filing_type: str
    filing_format: str
    status: str
    subject_name: str | None
    narrative: str | None
    created_by: str
    created_at: str
    updated_at: str


# ── In-memory store ──────────────────────────────────────────────────

_filings: dict[str, dict] = {}


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/sar")
async def list_filings(status: str | None = None):
    """List all SAR/STR filings."""
    filings = list(_filings.values())
    if status:
        filings = [f for f in filings if f["status"] == status]
    return {
        "filings": sorted(filings, key=lambda f: f["created_at"], reverse=True),
        "count": len(filings),
    }


@router.post("/sar", status_code=201)
async def create_filing(body: SARCreateRequest):
    """Create a new SAR/STR filing from an alert."""
    filing_id = str(uuid4())
    filing = {
        "id": filing_id,
        "alert_id": body.alert_id,
        "filing_type": "SAR",
        "filing_format": body.filing_format,
        "status": "DRAFT",
        "subject_name": None,
        "subject_account": None,
        "narrative": body.narrative,
        "structured_data": _generate_structured_data(body.filing_format, body.alert_id),
        "filed_at": None,
        "filed_by": None,
        "created_by": "analyst",
        "created_at": _now(),
        "updated_at": _now(),
    }
    _filings[filing_id] = filing
    return {"status": "created", "filing": filing}


@router.get("/sar/{filing_id}")
async def get_filing(filing_id: str):
    """Get a SAR filing by ID."""
    filing = _filings.get(filing_id)
    if not filing:
        raise HTTPException(status_code=404, detail="Filing not found")
    return filing


@router.patch("/sar/{filing_id}")
async def update_filing(filing_id: str, body: SARUpdateRequest):
    """Update SAR status/narrative (workflow: DRAFT → PENDING_REVIEW → APPROVED → FILED)."""
    filing = _filings.get(filing_id)
    if not filing:
        raise HTTPException(status_code=404, detail="Filing not found")

    # Validate status transitions
    valid_transitions = {
        "DRAFT": ["PENDING_REVIEW", "REJECTED"],
        "PENDING_REVIEW": ["APPROVED", "REJECTED", "DRAFT"],
        "APPROVED": ["FILED", "REJECTED"],
        "FILED": [],
        "REJECTED": ["DRAFT"],
    }
    current = filing["status"]
    if body.status and body.status not in valid_transitions.get(current, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot transition from {current} to {body.status}. "
                   f"Valid: {valid_transitions.get(current, [])}",
        )

    if body.status:
        filing["status"] = body.status
    if body.narrative is not None:
        filing["narrative"] = body.narrative
    if body.filed_by:
        filing["filed_by"] = body.filed_by
    if body.status == "FILED":
        filing["filed_at"] = _now()
    filing["updated_at"] = _now()

    return {"status": "updated", "filing": filing}


@router.get("/sar/{filing_id}/export")
async def export_filing(filing_id: str):
    """Export SAR in the specified filing format (FinCEN XML, FCA CSV, etc.)."""
    filing = _filings.get(filing_id)
    if not filing:
        raise HTTPException(status_code=404, detail="Filing not found")

    fmt = filing["filing_format"]
    if fmt == "FinCEN_BSA":
        return _export_fincen(filing)
    elif fmt == "FCA_UK":
        return _export_fca(filing)
    elif fmt == "BaFin_DE":
        return _export_bafin(filing)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported format: {fmt}")


@router.get("/stats")
async def reporting_stats():
    """Reporting statistics."""
    filings = list(_filings.values())
    return {
        "total_filings": len(filings),
        "by_status": _count_by(filings, "status"),
        "by_format": _count_by(filings, "filing_format"),
        "by_type": _count_by(filings, "filing_type"),
    }


@router.get("/feedback-metrics")
async def feedback_metrics(horizon_days: int = 30):
    """Gap 9 — per-factor chargeback / analyst feedback metrics.

    Joins each alert's final disposition (RESOLVED + filed SAR = TP, DISMISSED
    = FP) with the factors stored on its risk_score. Returns suggest-only
    'tighten' / 'loosen' advisories — never mutates detection_rules.
    """
    from app.services.feedback_loop import compute_from_postgres, demo_report

    try:
        import psycopg2
        import os as _os
        dsn = (
            f"postgresql://{_os.getenv('POSTGRES_USER','afds_admin')}:"
            f"{_os.getenv('POSTGRES_PASSWORD','afds_secret')}@"
            f"{_os.getenv('POSTGRES_HOST','localhost')}:"
            f"{_os.getenv('POSTGRES_PORT','5432')}/"
            f"{_os.getenv('POSTGRES_DB','afds')}"
        )
        conn = psycopg2.connect(dsn, connect_timeout=2)
        try:
            return compute_from_postgres(conn, horizon_days=horizon_days)
        finally:
            conn.close()
    except Exception as exc:
        logger.info("feedback-metrics falling back to demo report: %s", exc)
        return demo_report(horizon_days=horizon_days)


@router.get("/model-drift")
async def model_drift(hours: int = 24, model: str = "vae"):
    """Phase G1 — Population Stability Index for advisory model scores.

    Compares the trailing ``hours``-hour window of ``afds.model_scores``
    against the model's training-time baseline (``calibration.json``).
    Emits the same payload the nightly CronJob publishes to the
    ``afds.model.drift`` Kafka topic so dashboards / alerting have a
    pull-path that doesn't require Kafka connectivity.

    Returns ``{status: "unavailable"}`` when the baseline artifact or
    recent window is empty — the endpoint never 500s.
    """
    from app.services import drift as drift_svc
    import os as _os

    calib_env = {
        "vae": "AFDS_VAE_CALIBRATION_PATH",
        "gnn": "AFDS_GNN_CALIBRATION_PATH",
    }.get(model, "")
    baseline: list[float] = []
    if calib_env:
        baseline = drift_svc.load_baseline_from_calibration(_os.getenv(calib_env, ""))

    recent: list[float] = []
    try:
        import psycopg2  # type: ignore
        dsn = (
            f"postgresql://{_os.getenv('POSTGRES_USER','afds_admin')}:"
            f"{_os.getenv('POSTGRES_PASSWORD','afds_secret')}@"
            f"{_os.getenv('POSTGRES_HOST','localhost')}:"
            f"{_os.getenv('POSTGRES_PORT','5432')}/"
            f"{_os.getenv('POSTGRES_DB','afds')}"
        )
        conn = psycopg2.connect(dsn, connect_timeout=2)
        try:
            recent = drift_svc.load_recent_from_postgres(conn, model_name=model, hours=hours)
        finally:
            conn.close()
    except Exception as exc:
        logger.info("model-drift: PG unreachable (%s); baseline-only response", exc)

    report = drift_svc.build_report(
        baseline, recent,
        model_name=model,
        model_version="unknown",
    )
    return report.to_dict()


# ── Helpers ──────────────────────────────────────────────────────────

def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        v = item.get(key, "unknown")
        counts[v] = counts.get(v, 0) + 1
    return counts


def _generate_structured_data(fmt: str, alert_id: str) -> dict:
    """Generate format-specific structured data template."""
    base = {
        "alert_reference": alert_id,
        "generated_at": _now(),
    }
    if fmt == "FinCEN_BSA":
        return {
            **base,
            "form_type": "FinCEN SAR",
            "part_i": {"filing_institution": "Example Fintech Ltd", "ein": ""},
            "part_ii": {"subject": {}, "address": {}},
            "part_iii": {"suspicious_activity": {}},
            "part_iv": {"financial_institution": {}},
            "part_v": {"narrative": ""},
        }
    elif fmt == "FCA_UK":
        return {
            **base,
            "form_type": "FCA STR",
            "reporter": {"firm_name": "Example Fintech Ltd", "frn": ""},
            "subject": {},
            "activity": {},
        }
    elif fmt == "BaFin_DE":
        return {
            **base,
            "form_type": "BaFin Verdachtsmeldung",
            "melder": {"name": "Example Fintech Ltd"},
            "verdaechtige_person": {},
            "transaktion": {},
        }
    return base


def _export_fincen(filing: dict) -> dict:
    return {
        "format": "FinCEN_BSA_XML",
        "content_type": "application/xml",
        "data": {
            "BSAReport": {
                "Activity": {
                    "AlertRef": filing.get("alert_id"),
                    "Subject": filing.get("subject_name"),
                    "Narrative": filing.get("narrative", ""),
                },
                "FilingDate": filing.get("filed_at"),
                "Status": filing["status"],
            }
        },
    }


def _export_fca(filing: dict) -> dict:
    return {
        "format": "FCA_UK_CSV",
        "content_type": "text/csv",
        "data": {
            "report_type": "STR",
            "firm": "Example Fintech Ltd",
            "subject": filing.get("subject_name"),
            "narrative": filing.get("narrative", ""),
            "status": filing["status"],
        },
    }


def _export_bafin(filing: dict) -> dict:
    return {
        "format": "BaFin_DE_XML",
        "content_type": "application/xml",
        "data": {
            "Verdachtsmeldung": {
                "Melder": "Example Fintech Ltd",
                "Betreff": filing.get("subject_name"),
                "Beschreibung": filing.get("narrative", ""),
                "Status": filing["status"],
            }
        },
    }
