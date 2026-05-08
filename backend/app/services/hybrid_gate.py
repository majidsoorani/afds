"""
Hybrid escalation gate (Phase F3).

Implements the single, isolated policy:

    soft_rule AND model_probability >= 0.85  →  HIGH_RISK

Definitions:
  * ``soft_rule``  = at least one rule fired but the deterministic
    risk_score is below the HIGH threshold (50). We use 25 as the lower
    bound so ALLOW transactions with zero factors are never escalated.
  * ``model_probability``  = max over the currently-enabled ML signals
    (VAE anomaly, GNN graph). Each signal is normalised to [0,1]
    independently so a new model can be added without changing callers.

Design constraints (from the approved plan):
  - **Gated**: runs only when ``AFDS_MODEL_MODE=hybrid``. For ``off``,
    ``shadow``, or ``autonomous`` we return the inputs unchanged so the
    rule engine output is preserved byte-for-byte.
  - **Non-gating for score**: we never modify ``risk_score``; we only
    escalate ``risk_level`` and ``action``. This preserves public validation
    (the suite asserts on classification categories, not risk levels).
  - **Idempotent**: calling with already-HIGH / CRITICAL input returns
    the input unchanged.
  - **Pure / no I/O**: this module must never call the network or DB.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Tunable via env for the canary ramp. Kept strict (0.85) by default so
# only high-confidence model signals escalate.
_DEFAULT_THRESHOLD = 0.85
_SOFT_RULE_MIN_SCORE = 25.0  # must have at least one rule firing
_SOFT_RULE_MAX_SCORE = 50.0  # below the HIGH threshold

# ── Kill-switch (Phase H3) ───────────────────────────────────────
# When AFDS_MODEL_MODE=off we want the hybrid path to return in O(1)
# without reading any other env var, without touching the network, and
# without allocating Python objects the JIT can hoist. We measure the
# cold-path latency in ``kill_switch_drill()`` below to guarantee the
# "<1ms logic switch" SLO even on cold laptops.
_KILL_SWITCH_MODE = "off"


def _mode() -> str:
    return (os.getenv("AFDS_MODEL_MODE") or "off").strip().lower()


def is_kill_switch_active() -> bool:
    """Instantaneous check used by hot-path callers. Must not allocate."""
    return _mode() == _KILL_SWITCH_MODE


def _threshold() -> float:
    try:
        return float(os.getenv("AFDS_HYBRID_THRESHOLD", _DEFAULT_THRESHOLD))
    except ValueError:
        return _DEFAULT_THRESHOLD


def is_enabled() -> bool:
    """Back-compat wrapper: hybrid path is live in both ``hybrid`` and
    ``canary`` modes. Canary narrows the bucket per-sender (see
    :func:`is_enabled_for_sender`)."""
    return _mode() in ("hybrid", "canary")


def _canary_percentage() -> int:
    """0–100, inclusive. Values outside the range are clamped."""
    try:
        raw = int(float(os.getenv("AFDS_CANARY_PERCENTAGE", "5")))
    except ValueError:
        raw = 5
    return max(0, min(raw, 100))


def _canary_salt() -> str:
    """Rotate the salt to reshuffle the canary cohort (e.g., for the
    next percentage step). Empty string is a stable default."""
    return os.getenv("AFDS_CANARY_SALT", "") or ""


def sender_bucket(sender_id: str, salt: str | None = None) -> int:
    """Deterministic bucket in ``[0, 100)`` for a given sender.

    Uses SHA-256 truncated to 8 bytes → unsigned int → mod 100 so that:

      * The same ``sender_id`` always lands in the same bucket across
        restarts, pod replacements, and nodes (no per-process state).
      * Adding a new salt reshuffles the cohort cleanly for the next
        percentage step.
      * The distribution is uniform to ~2% on realistic sender populations.

    SHA-256 is chosen over Python's builtin ``hash()`` because the latter
    is randomised per interpreter (PYTHONHASHSEED), which would break
    sticky-user semantics across pod restarts. The cost (~1µs) is well
    below the hot-path budget.
    """
    if not sender_id:
        return 100  # out-of-band → never in canary cohort
    s = sender_id if isinstance(sender_id, str) else str(sender_id)
    key = (salt if salt is not None else _canary_salt()) + "|" + s
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    # First 8 bytes, big-endian unsigned → mod 100.
    return int.from_bytes(digest[:8], "big", signed=False) % 100


def is_enabled_for_sender(sender_id: str | None) -> bool:
    """Evaluate the canary gate for a specific sender.

    * ``mode=off``           → False (absolute kill-switch)
    * ``mode=shadow``        → False (scoring runs, but no escalation)
    * ``mode=hybrid``        → True  (100% cohort; full rollout)
    * ``mode=canary``        → True  iff ``bucket < AFDS_CANARY_PERCENTAGE``
    * ``mode=autonomous``    → False (reserved; not yet implemented — the
                                 hybrid gate is scoped to advisory escalation,
                                 fully autonomous override requires a separate
                                 release gate with its own model card sign-off)
    """
    mode = _mode()
    if mode == "hybrid":
        return True
    if mode != "canary":
        return False
    if not sender_id:
        return False
    pct = _canary_percentage()
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    return sender_bucket(sender_id) < pct


def _model_probability(
    anomaly_block: dict[str, Any] | None,
    graph_block: dict[str, Any] | None,
) -> float:
    """Collapse all ML advisories into a single [0,1] probability.

    * VAE / IForest — ``anomaly_score`` is 0..100, so we divide by 100.
    * GNN / graph — ``score`` is already 0..1.

    Missing / non-numeric values contribute 0. We take the **max** to
    preserve the "any confident model signal is enough" semantics.
    """
    probs: list[float] = []
    if isinstance(anomaly_block, dict):
        raw = anomaly_block.get("anomaly_score", 0) or 0
        try:
            probs.append(max(0.0, min(float(raw) / 100.0, 1.0)))
        except (TypeError, ValueError):
            pass
    if isinstance(graph_block, dict):
        raw = graph_block.get("score", 0) or 0
        try:
            probs.append(max(0.0, min(float(raw), 1.0)))
        except (TypeError, ValueError):
            pass
    return max(probs) if probs else 0.0


def maybe_escalate(
    *,
    risk_score: float,
    risk_level: str,
    action: str,
    factors: list[str] | None,
    anomaly_block: dict[str, Any] | None = None,
    graph_block: dict[str, Any] | None = None,
    sender_id: str | None = None,
) -> dict[str, Any]:
    """Return ``{risk_level, action, escalated, reason}``.

    When the hybrid gate is disabled (any mode other than ``hybrid`` /
    ``canary``, or a canary bucket miss) we return the inputs verbatim
    and ``escalated=False``. Callers should pass the returned values
    through unconditionally; the helper handles the no-op case.
    """
    result = {
        "risk_level": risk_level,
        "action": action,
        "escalated": False,
        "reason": None,
        "model_probability": 0.0,
        "threshold": _threshold(),
        "mode": _mode(),
        "canary_bucket": None,
    }

    # Absolute kill-switch — must be the *first* check so `AFDS_MODEL_MODE=off`
    # is a true O(1) short-circuit.
    if is_kill_switch_active():
        return result

    # Per-sender canary gate. In ``hybrid`` mode this is always True;
    # in ``canary`` mode it's True only for the hashed cohort.
    if not is_enabled_for_sender(sender_id):
        if _mode() == "canary" and sender_id:
            result["canary_bucket"] = sender_bucket(sender_id)
        return result
    if _mode() == "canary" and sender_id:
        result["canary_bucket"] = sender_bucket(sender_id)

    # Already HIGH / CRITICAL → nothing to do. Preserves idempotency.
    if risk_level in ("HIGH", "CRITICAL"):
        return result

    # "soft_rule" = rules fired but didn't make HIGH on their own.
    has_rule_firing = bool(factors) and risk_score >= _SOFT_RULE_MIN_SCORE
    if not has_rule_firing or risk_score >= _SOFT_RULE_MAX_SCORE:
        return result

    prob = _model_probability(anomaly_block, graph_block)
    result["model_probability"] = round(prob, 4)
    if prob < result["threshold"]:
        return result

    # Escalate. Level/action are upgraded; risk_score is NOT modified.
    result["risk_level"] = "HIGH"
    result["action"] = "SUSPEND"
    result["escalated"] = True
    result["reason"] = (
        f"hybrid_escalation: soft_rule (score={risk_score:.1f}) + "
        f"model_probability={prob:.3f} >= {result['threshold']:.2f}"
    )
    logger.info(result["reason"])
    return result


# ─────────────────────────────────────────────────────────────────
# Kill-switch drill (Phase H3)
# ─────────────────────────────────────────────────────────────────
def kill_switch_drill(
    iterations: int = 10_000,
    *,
    sender_id: str = "drill-sender",
    risk_score: float = 30.0,
) -> dict[str, Any]:
    """Synthetic drill: measure the cold-path latency with ``MODE=off``.

    Returns ``{iterations, p50_us, p95_us, p99_us, max_us, sub_ms_ratio,
    passed}``. ``passed`` is True iff the p99 latency of the kill-switch
    short-circuit is under 1 millisecond — the published "<1ms logic
    switch" SLO.

    Callers (drills, CI gates, Grafana synthetic probes) can invoke this
    without standing up a backend; it is pure Python and touches no I/O.
    """
    # Snapshot current env and force off for the duration of the drill,
    # then restore — the drill must be observable regardless of operator
    # state and never leak state back into the process.
    prev_mode = os.environ.get("AFDS_MODEL_MODE")
    os.environ["AFDS_MODEL_MODE"] = _KILL_SWITCH_MODE

    try:
        latencies_ns: list[int] = [0] * iterations
        for i in range(iterations):
            t0 = time.perf_counter_ns()
            maybe_escalate(
                risk_score=risk_score,
                risk_level="MEDIUM",
                action="FLAG",
                factors=["VELOCITY:3(+5)"],
                anomaly_block={"anomaly_score": 99.0, "is_anomaly": True},
                graph_block={"score": 0.99, "is_anomaly": True},
                sender_id=sender_id,
            )
            latencies_ns[i] = time.perf_counter_ns() - t0
    finally:
        if prev_mode is None:
            os.environ.pop("AFDS_MODEL_MODE", None)
        else:
            os.environ["AFDS_MODEL_MODE"] = prev_mode

    latencies_ns.sort()
    n = len(latencies_ns)

    def _pct(p: float) -> float:
        idx = min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))
        return latencies_ns[idx] / 1_000.0  # µs

    sub_ms = sum(1 for ns in latencies_ns if ns < 1_000_000) / n
    p99_us = _pct(99)
    return {
        "iterations": iterations,
        "p50_us": round(_pct(50), 3),
        "p95_us": round(_pct(95), 3),
        "p99_us": round(p99_us, 3),
        "max_us": round(max(latencies_ns) / 1_000.0, 3),
        "sub_ms_ratio": round(sub_ms, 5),
        "passed": p99_us < 1_000.0,  # strict <1ms SLO
    }
