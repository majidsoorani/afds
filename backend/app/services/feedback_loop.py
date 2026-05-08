"""Gap 9 — Chargeback / analyst feedback loop.

Joins risk_scores.factors with each alert's final disposition to compute
per-factor precision and emit suggest-only threshold tuning advisories.

Disposition mapping:
  TP (true-positive)  = alert status RESOLVED  OR linked SAR filed (status != DRAFT/REJECTED)
  FP (false-positive) = alert status DISMISSED
  Unclear             = OPEN / INVESTIGATING   (excluded)

This module is advisory-only: it never mutates detection_rules. The nightly
job writes the JSON report to stdout (scraped by Loki/Promtail); the
`/reporting/feedback-metrics` endpoint re-computes on demand.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, asdict
from typing import Iterable

logger = logging.getLogger(__name__)

# Minimum sample size before we trust a precision figure
_MIN_SAMPLES = int(os.getenv("AFDS_FEEDBACK_MIN_SAMPLES", "20"))

# Precision thresholds for advisories
_LOW_PRECISION = float(os.getenv("AFDS_FEEDBACK_LOW_PRECISION", "0.30"))
_HIGH_PRECISION = float(os.getenv("AFDS_FEEDBACK_HIGH_PRECISION", "0.85"))


@dataclass
class FactorStat:
    factor: str
    tp: int
    fp: int
    unclear: int
    precision: float | None
    suggestion: str  # "tighten" | "loosen" | "hold" | "insufficient_data"


def _suggestion(tp: int, fp: int) -> tuple[float | None, str]:
    total = tp + fp
    if total < _MIN_SAMPLES:
        return None, "insufficient_data"
    prec = tp / total
    if prec < _LOW_PRECISION:
        return prec, "loosen"       # mostly false positives → raise threshold / drop weight
    if prec > _HIGH_PRECISION:
        return prec, "tighten"      # mostly real fraud → lower threshold / raise weight
    return prec, "hold"


def compute_from_rows(rows: Iterable[dict]) -> list[FactorStat]:
    """Aggregate per-factor TP/FP from joined rows.

    Each row: {factor: str, disposition: 'TP'|'FP'|'UNCLEAR'}
    """
    agg: dict[str, dict[str, int]] = {}
    for r in rows:
        f = r["factor"]
        d = r["disposition"]
        bucket = agg.setdefault(f, {"tp": 0, "fp": 0, "unclear": 0})
        if d == "TP":
            bucket["tp"] += 1
        elif d == "FP":
            bucket["fp"] += 1
        else:
            bucket["unclear"] += 1

    out: list[FactorStat] = []
    for factor, b in sorted(agg.items()):
        prec, sug = _suggestion(b["tp"], b["fp"])
        out.append(FactorStat(
            factor=factor,
            tp=b["tp"],
            fp=b["fp"],
            unclear=b["unclear"],
            precision=round(prec, 3) if prec is not None else None,
            suggestion=sug,
        ))
    return out


_DEMO_FACTORS = [
    ("VELOCITY_HIGH", 0.92),
    ("AMOUNT_HIGH", 0.88),
    ("COP_AC01", 0.95),
    ("COP_ANNM", 0.72),
    ("PATTERN_TESTING", 0.81),
    ("ENTITY_SANCTIONED", 0.99),
    ("ENTITY_FRAUD_KEYWORD", 0.45),
    ("DUPLICATE_TRANSACTION", 0.58),
    ("GEO_MISMATCH", 0.66),
    ("ML_ANOMALY", 0.22),  # advisory-only, expected to be noisy
    ("NO_SOCIAL_FOOTPRINT", 0.18),
    ("YOUNG_DOMAIN", 0.75),
]


def demo_report(seed: int = 42, horizon_days: int = 30) -> dict:
    """Offline deterministic report for demos / unit tests."""
    rng = random.Random(seed)
    rows: list[dict] = []
    for factor, true_prec in _DEMO_FACTORS:
        n = rng.randint(25, 180)
        for _ in range(n):
            if rng.random() < true_prec:
                rows.append({"factor": factor, "disposition": "TP"})
            else:
                rows.append({"factor": factor, "disposition": "FP"})
    stats = compute_from_rows(rows)
    return {
        "horizon_days": horizon_days,
        "min_samples": _MIN_SAMPLES,
        "low_precision_threshold": _LOW_PRECISION,
        "high_precision_threshold": _HIGH_PRECISION,
        "source": "demo",
        "factors": [asdict(s) for s in stats],
        "summary": _summary(stats),
    }


def _summary(stats: list[FactorStat]) -> dict:
    return {
        "total_factors": len(stats),
        "tighten": [s.factor for s in stats if s.suggestion == "tighten"],
        "loosen": [s.factor for s in stats if s.suggestion == "loosen"],
        "hold": [s.factor for s in stats if s.suggestion == "hold"],
        "insufficient_data": [s.factor for s in stats if s.suggestion == "insufficient_data"],
    }


# ── Live PG path (used by the cronjob and API when DB is reachable) ──

_SQL = """
WITH classified AS (
    SELECT a.id AS alert_id,
           rs.factors,
           CASE
             WHEN a.status = 'RESOLVED' THEN 'TP'
             WHEN a.status = 'DISMISSED' THEN 'FP'
             WHEN EXISTS (
               SELECT 1 FROM afds.sar_filings sf
               WHERE sf.alert_id = a.id AND sf.status NOT IN ('DRAFT','REJECTED')
             ) THEN 'TP'
             ELSE 'UNCLEAR'
           END AS disposition
    FROM afds.alerts a
    LEFT JOIN afds.risk_scores rs ON rs.id = a.risk_score_id
    WHERE a.created_at >= NOW() - (%s::int || ' days')::interval
      AND rs.factors IS NOT NULL
)
SELECT jsonb_array_elements(factors)::text AS factor_raw,
       disposition
FROM classified
WHERE disposition IN ('TP','FP');
"""


def compute_from_postgres(conn, horizon_days: int = 30) -> dict:
    """Run the join on a live connection. Returns the same shape as demo_report()."""
    cur = conn.cursor()
    cur.execute(_SQL, (horizon_days,))
    rows: list[dict] = []
    for raw, disp in cur.fetchall():
        # factors stored as JSONB array of strings like "VELOCITY_HIGH:8(+10.0)"
        # collapse to the factor name (before the first colon)
        factor = str(raw).strip('"').split(":", 1)[0]
        rows.append({"factor": factor, "disposition": disp})
    stats = compute_from_rows(rows)
    report = {
        "horizon_days": horizon_days,
        "min_samples": _MIN_SAMPLES,
        "low_precision_threshold": _LOW_PRECISION,
        "high_precision_threshold": _HIGH_PRECISION,
        "source": "postgres",
        "factors": [asdict(s) for s in stats],
        "summary": _summary(stats),
    }
    # Phase G1 — attach model drift (PSI) so a single feedback-loop run
    # surfaces both the rule-engine precision and the ML advisory drift.
    # Never raises into the caller; drift attachment is strictly
    # additive and failures fall back to ``status="unavailable"``.
    try:
        report["model_drift"] = _attach_drift_from_postgres(conn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("drift attachment skipped: %s", exc)
        report["model_drift"] = {"status": "unavailable", "reason": str(exc)[:120]}
    return report


def _attach_drift_from_postgres(conn) -> dict:
    """Build and (optionally) emit the drift report for each enabled model.

    Looks up baselines from ``AFDS_VAE_CALIBRATION_PATH`` (and sibling
    paths by model name). The Kafka emission is best-effort — if the
    producer is absent (laptop / tests) we still return the report body.
    """
    from app.services import drift  # lazy: keeps import cost off hot path

    hours = int(os.getenv("AFDS_DRIFT_RECENT_HOURS", "24"))
    reports: dict[str, dict] = {}
    for model_name, env_key, default_version in (
        ("vae", "AFDS_VAE_CALIBRATION_PATH", "unknown"),
        ("gnn", "AFDS_GNN_CALIBRATION_PATH", "unknown"),
    ):
        calib_path = os.getenv(env_key, "").strip()
        baseline: list[float] = []
        if calib_path:
            baseline = drift.load_baseline_from_calibration(calib_path)
        if not baseline:
            reports[model_name] = {
                "status": "unavailable",
                "psi": 0.0,
                "reason": "baseline_missing",
            }
            continue
        recent = drift.load_recent_from_postgres(conn, model_name=model_name, hours=hours)
        report = drift.build_report(
            baseline, recent,
            model_name=model_name,
            model_version=default_version,
        )
        drift.emit_to_kafka(report)  # best-effort — never blocks
        reports[model_name] = report.to_dict()
    return reports
