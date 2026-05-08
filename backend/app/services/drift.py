"""
Model drift detection (Phase G1) — Population Stability Index.

Compares a recent rolling window of ``afds.model_scores.model_score``
against the training-time baseline distribution captured in the model
artifact's ``calibration.json``. Emits a JSON report and (optionally)
a Kafka message to ``afds.model.drift``.

PSI interpretation (industry standard):

    PSI < 0.10   → stable
    0.10 ≤ PSI < 0.25  → moderate drift (monitor)
    PSI ≥ 0.25   → material drift (alert)

The ``AFDS_DRIFT_ALERT_THRESHOLD`` env var (default 0.20) sets the
Grafana alerting threshold. Computation is pure Python — no numpy
required — so this module is safe to run inside the nightly
CronJob container without ML dependencies.

Design invariants:

* **Never raises** into the cronjob; all failures collapse to an
  empty report with ``status="unavailable"``.
* **Bin alignment**: we discretise both distributions onto the
  baseline's quantile edges so PSI is comparable across runs.
* **Smoothing**: any bin with 0 observations is floored to
  ``1 / total`` to avoid ``log(0)`` blow-ups (a standard trick).
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, asdict
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

_DEFAULT_BINS = 10
_DEFAULT_THRESHOLD = float(os.getenv("AFDS_DRIFT_ALERT_THRESHOLD", "0.20"))
_EPSILON_FLOOR = 1e-6  # replaces true zeros before log()


@dataclass
class BinStat:
    lo: float
    hi: float
    baseline_frac: float
    recent_frac: float
    psi_contribution: float


@dataclass
class DriftReport:
    model_name: str
    model_version: str
    psi: float
    status: str  # "stable" | "monitor" | "alert" | "unavailable"
    n_baseline: int
    n_recent: int
    threshold: float
    bins: list[BinStat]

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in asdict(self).items() if k != "bins"},
            "bins": [asdict(b) for b in self.bins],
        }


# ─────────────────────────────────────────────────────────────────
# Pure PSI math — fully unit-testable
# ─────────────────────────────────────────────────────────────────
def _quantile_edges(values: Sequence[float], bins: int) -> list[float]:
    """Return ``bins+1`` edges that partition ``values`` into equal-count bins.

    Uses nearest-rank quantiles (the variant most robust to ties) and
    deduplicates collapsed edges to avoid zero-width bins.
    """
    if not values:
        return []
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    edges = [sorted_vals[0]]
    for i in range(1, bins):
        idx = max(0, min(n - 1, int(math.floor(i * n / bins))))
        edges.append(sorted_vals[idx])
    edges.append(sorted_vals[-1])
    # Deduplicate while preserving order so collapsed quantiles don't
    # produce zero-width bins (a common failure mode on skewed scores).
    deduped: list[float] = []
    for e in edges:
        if not deduped or e > deduped[-1]:
            deduped.append(e)
    # Always return at least 2 edges so bin-assign has somewhere to go.
    if len(deduped) < 2:
        deduped.append(deduped[-1] + 1e-9)
    return deduped


def _bin_fractions(values: Sequence[float], edges: Sequence[float]) -> list[float]:
    """Fraction of ``values`` falling in each ``(edges[i], edges[i+1]]`` bucket."""
    if not values or len(edges) < 2:
        return []
    counts = [0] * (len(edges) - 1)
    last = len(edges) - 1
    for v in values:
        # Binary search for rightmost edge <= v.
        lo, hi = 0, last
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if edges[mid] <= v:
                lo = mid
            else:
                hi = mid - 1
        # Values below the first edge land in bucket 0; above the last
        # edge land in bucket ``last-1``.
        bucket = min(max(lo, 0), last - 1)
        counts[bucket] += 1
    total = sum(counts)
    if total == 0:
        return [0.0] * len(counts)
    return [c / total for c in counts]


def compute_psi(
    baseline: Sequence[float],
    recent: Sequence[float],
    bins: int = _DEFAULT_BINS,
) -> tuple[float, list[BinStat]]:
    """Return ``(psi, per_bin_stats)``.

    PSI is symmetric in the absolute-contribution sense:
        PSI = Σ (p_recent - p_baseline) · ln(p_recent / p_baseline)

    Empty inputs collapse to ``(0.0, [])`` so callers can treat
    "no data yet" as "no drift".
    """
    if not baseline or not recent:
        return 0.0, []

    edges = _quantile_edges(baseline, bins=bins)
    base_fracs = _bin_fractions(baseline, edges)
    rec_fracs = _bin_fractions(recent, edges)

    stats: list[BinStat] = []
    psi_total = 0.0
    for i, (b, r) in enumerate(zip(base_fracs, rec_fracs)):
        b_safe = b if b > 0 else _EPSILON_FLOOR
        r_safe = r if r > 0 else _EPSILON_FLOOR
        contrib = (r_safe - b_safe) * math.log(r_safe / b_safe)
        psi_total += contrib
        stats.append(
            BinStat(
                lo=round(edges[i], 6),
                hi=round(edges[i + 1], 6),
                baseline_frac=round(b, 6),
                recent_frac=round(r, 6),
                psi_contribution=round(contrib, 6),
            )
        )
    return round(psi_total, 6), stats


def classify(psi: float, threshold: float = _DEFAULT_THRESHOLD) -> str:
    if psi >= threshold:
        return "alert"
    if psi >= 0.10:
        return "monitor"
    return "stable"


# ─────────────────────────────────────────────────────────────────
# Data loaders (baseline from calibration.json, recent from Postgres)
# ─────────────────────────────────────────────────────────────────
def load_baseline_from_calibration(path: str) -> list[float]:
    """Read ``reconstruction_errors`` / ``baseline_scores`` from a training
    artifact's ``calibration.json``. Returns ``[]`` on any failure."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        logger.debug("baseline load failed (%s)", exc)
        return []
    for key in ("baseline_scores", "reconstruction_errors"):
        values = payload.get(key)
        if isinstance(values, list):
            return [float(v) for v in values if isinstance(v, (int, float))]
    return []


_RECENT_SQL = """
SELECT model_score
FROM afds.model_scores
WHERE created_at >= NOW() - (%s::int || ' hours')::interval
  AND source = 'model'
  AND model_name = %s
"""


def load_recent_from_postgres(
    conn, model_name: str = "vae", hours: int = 24
) -> list[float]:
    """Pull the trailing window of advisory model scores.

    Filters ``source = 'model'`` so the safe-default rows emitted by the
    Flink operator on timeout / error never contaminate the drift signal.
    """
    cur = conn.cursor()
    cur.execute(_RECENT_SQL, (hours, model_name))
    return [float(row[0]) for row in cur.fetchall() if row and row[0] is not None]


# ─────────────────────────────────────────────────────────────────
# High-level report builder
# ─────────────────────────────────────────────────────────────────
def build_report(
    baseline: Sequence[float],
    recent: Sequence[float],
    *,
    model_name: str,
    model_version: str,
    bins: int = _DEFAULT_BINS,
    threshold: float = _DEFAULT_THRESHOLD,
) -> DriftReport:
    if not baseline or not recent:
        return DriftReport(
            model_name=model_name,
            model_version=model_version,
            psi=0.0,
            status="unavailable",
            n_baseline=len(baseline),
            n_recent=len(recent),
            threshold=threshold,
            bins=[],
        )
    psi, stats = compute_psi(baseline, recent, bins=bins)
    return DriftReport(
        model_name=model_name,
        model_version=model_version,
        psi=psi,
        status=classify(psi, threshold=threshold),
        n_baseline=len(baseline),
        n_recent=len(recent),
        threshold=threshold,
        bins=stats,
    )


# ─────────────────────────────────────────────────────────────────
# Kafka emission (best-effort, never blocks the caller on failure)
# ─────────────────────────────────────────────────────────────────
def emit_to_kafka(report: DriftReport, topic: str = "afds.model.drift") -> bool:
    """Return True iff we successfully enqueued the report.

    Uses the existing ``app.core.kafka`` producer when available and
    silently no-ops when Kafka is absent (laptop / unit tests).
    """
    try:
        from app.core.kafka import get_producer  # type: ignore
    except ImportError:
        logger.debug("Kafka producer unavailable; skipping drift emission")
        return False
    try:
        producer = get_producer()
        if producer is None:
            return False
        producer.send(topic, report.to_dict())
        producer.flush(timeout=2.0)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("drift kafka emit failed: %s", exc)
        return False
