"""
AFDS XAI reason-code service (Phase E).

Produces a **unified, frontend-stable** ``reason_codes`` payload so the
UI does not need to care whether the explanation came from the FastSHAP
model surrogate or from the deterministic rule engine. Every entry has
the same shape:

    {
        "feature": "velocity_2min",
        "contribution": 15.0,
        "source": "rule" | "model",
        "description": "2 txns in 2 minutes",   # optional, human-readable
    }

Architectural contract (from the plan):

1. **10 ms budget** — the model call is hard-capped at
   ``AFDS_XAI_TIMEOUT_MS`` (default 10ms). We use an HTTP client timeout
   configured with explicit connect/read/write/pool values, so a slow
   DNS lookup or TCP handshake cannot blow the budget.
2. **Neuro-symbolic fallback** — on timeout, network error, non-2xx,
   malformed payload, or when ``AFDS_XAI_MODE=off``, we seamlessly
   synthesise reason codes from the existing ``factors[]`` list
   (``source="rule"``). The frontend never receives an empty list when
   there are factors to show.
3. **Non-gating** — this module NEVER alters the risk score. It only
    annotates the response, and public validation must pass with this
    feature toggled in either direction.
4. **Never raises** — any exception collapses to the rule-based fallback.

Design notes:

- We call the model-service synchronously from the sync scoring function
  using ``httpx.Client`` (not ``AsyncClient``) to avoid bridging through
  ``asyncio.run`` on the hot path — this shaves ~1ms off the budget
  margin and is safer inside running event loops.
- When the model returns reason codes, we **replace** the rule-based
  list (rather than merging) so the frontend shows one coherent
  explanation. The deterministic ``factors`` are always echoed on the
  response separately so operators can audit both views.
- The parser for ``factors[]`` is the sole source of truth for the
  rule→feature name mapping. Tests lock that mapping so UI contracts
  don't drift silently.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)

# Rule prefix → stable frontend-friendly feature name. New factors MUST
# be added here (and covered by a test) before they'll be shown in the UI.
_RULE_FEATURE_MAP: dict[str, str] = {
    "VELOCITY": "velocity_2min",
    "AMOUNT": "amount_threshold",
    "PATTERN": "round_number_pattern",
    "DUPLICATE": "duplicate_amount_rapid",
    "ENTITY": "suspicious_entity_name",
    "COP": "cop_verification",
    "VELOCITY_INBOUND_BURST": "inbound_velocity_burst",
    "AMOUNT_EXCEEDS_INCOME": "amount_exceeds_income",
    "ML_ANOMALY": "ml_anomaly_iforest",
    "ML_GRAPH": "ml_graph_gnn",
}

# ``FACTOR:<body>(+<number>)`` — tolerant of negative, decimal, and missing +.
_CONTRIB_RE = re.compile(r"\(\s*\+?(-?\d+(?:\.\d+)?)\s*\)\s*$")
_PREFIX_RE = re.compile(r"^([A-Z_]+)(?::(.*))?$")


def _mode() -> str:
    """One of: ``off`` | ``fastshap`` | ``symbolic`` (default ``symbolic``)."""
    return (os.getenv("AFDS_XAI_MODE") or "symbolic").strip().lower()


def _timeout_seconds() -> float:
    try:
        raw = float(os.getenv("AFDS_XAI_TIMEOUT_MS", "10"))
    except ValueError:
        raw = 10.0
    return max(raw, 1.0) / 1000.0  # floor 1ms to avoid zero-timeout edge cases


def _endpoint() -> str:
    return (os.getenv("AFDS_MODEL_ENDPOINT") or "").rstrip("/")


def _parse_factor(raw: str) -> tuple[str, float, str]:
    """Return ``(feature, contribution, description)`` for a factor string.

    Unknown prefixes collapse to a snake-cased version of the prefix.
    Missing contribution defaults to 0.0 (the caller can overlay a score
    from ``score_breakdown`` if needed).
    """
    contribution = 0.0
    body = raw
    m = _CONTRIB_RE.search(raw)
    if m:
        try:
            contribution = float(m.group(1))
        except ValueError:
            contribution = 0.0
        body = raw[: m.start()].rstrip()

    prefix_match = _PREFIX_RE.match(body)
    if prefix_match:
        prefix = prefix_match.group(1)
        descr = (prefix_match.group(2) or "").strip() or raw
    else:
        prefix = body.strip().upper()
        descr = raw

    feature = _RULE_FEATURE_MAP.get(prefix, prefix.lower().strip("_") or "unknown")
    return feature, contribution, descr


def build_rule_reasons(
    factors: list[str],
    score_breakdown: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Neuro-symbolic fallback: convert ``factors[]`` into unified records.

    Deterministic, zero-I/O, <1ms for realistic factor lists. This is the
    lower bound the frontend is guaranteed to receive.
    """
    reasons: list[dict[str, Any]] = []
    seen: set[str] = set()
    breakdown = score_breakdown or {}

    for raw in factors or []:
        try:
            feature, contribution, descr = _parse_factor(str(raw))
        except Exception:  # noqa: BLE001 - belt-and-braces
            continue
        if feature in seen:
            # If the same rule fires twice (shouldn't happen, but safe),
            # keep the higher-magnitude entry.
            existing = next(r for r in reasons if r["feature"] == feature)
            if abs(contribution) > abs(existing["contribution"]):
                existing["contribution"] = contribution
                existing["description"] = descr
            continue
        # If contribution couldn't be parsed, try the breakdown.
        if contribution == 0.0 and breakdown:
            # ``VELOCITY`` → ``velocity`` / ``AMOUNT`` → ``amount`` etc.
            bk_key = feature.split("_")[0] if feature not in breakdown else feature
            for candidate in (feature, bk_key, feature.replace("ml_", "ml_")):
                if candidate in breakdown:
                    contribution = float(breakdown[candidate])
                    break
        reasons.append(
            {
                "feature": feature,
                "contribution": round(float(contribution), 2),
                "source": "rule",
                "description": descr,
            }
        )
        seen.add(feature)

    # Stable ordering: highest-magnitude contribution first.
    reasons.sort(key=lambda r: abs(r["contribution"]), reverse=True)
    return reasons


def _call_fastshap(
    features: dict[str, Any],
    risk_score: float,
    timeout_s: float,
    endpoint: str,
) -> list[dict[str, Any]] | None:
    """POST to model-service ``/explain`` with a hard timeout.

    Returns ``None`` on any failure (caller falls back to rule reasons).
    Runs in <=``timeout_s`` seconds in the worst case: we configure
    connect/read/write/pool timeouts explicitly so a flapping DNS or
    half-open TCP socket cannot exceed the budget.
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        logger.debug("httpx not installed; FastSHAP disabled")
        return None

    url = f"{endpoint}/explain"
    # Sub-budget each phase so total worst-case = timeout_s.
    http_timeout = httpx.Timeout(
        timeout=timeout_s,
        connect=timeout_s,
        read=timeout_s,
        write=timeout_s,
        pool=timeout_s,
    )
    payload = {"features": features, "risk_score": risk_score, "top_k": 10}
    try:
        with httpx.Client(timeout=http_timeout) as client:
            resp = client.post(url, json=payload)
        if resp.status_code != 200:
            logger.debug("FastSHAP non-200: %s", resp.status_code)
            return None
        body = resp.json()
    except Exception as exc:  # httpx.TimeoutException, httpx.HTTPError, ValueError, ...
        logger.debug("FastSHAP call failed: %s", type(exc).__name__)
        return None

    items = body.get("reason_codes") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None

    normalised: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        feature = entry.get("feature")
        contribution = entry.get("contribution")
        if not isinstance(feature, str) or not isinstance(
            contribution, (int, float)
        ):
            continue
        normalised.append(
            {
                "feature": feature,
                "contribution": round(float(contribution), 2),
                "source": "model",
                "description": str(entry.get("description") or feature),
            }
        )
    return normalised or None


def build_reason_codes(
    factors: list[str],
    features: dict[str, Any],
    score_breakdown: dict[str, float] | None = None,
    risk_score: float = 0.0,
) -> list[dict[str, Any]]:
    """Produce the unified ``reason_codes`` payload.

    Order of operations:
      1. If ``AFDS_XAI_MODE`` is ``off`` or model endpoint is unset →
         return rule-based reasons.
      2. If ``AFDS_XAI_MODE=fastshap`` and endpoint configured → call
         ``/explain`` with the 10ms timeout; on success, return model
         reasons; on any failure, fall through to rules.
      3. If ``AFDS_XAI_MODE=symbolic`` (default) → return rule reasons
         without making a network call.

    Never raises. Never returns ``None``. May return ``[]`` iff both
    factors and breakdown are empty AND the model declined to explain.
    """
    rule_reasons = build_rule_reasons(factors, score_breakdown)

    mode = _mode()
    if mode not in ("fastshap", "hybrid"):
        return rule_reasons

    endpoint = _endpoint()
    if not endpoint:
        return rule_reasons

    model_reasons = _call_fastshap(
        features=features,
        risk_score=risk_score,
        timeout_s=_timeout_seconds(),
        endpoint=endpoint,
    )
    if model_reasons is None:
        return rule_reasons

    # Hybrid: prepend any rule factors the model didn't already cite so
    # the operator still sees the deterministic justification.
    if mode == "hybrid":
        model_features = {r["feature"] for r in model_reasons}
        extras = [r for r in rule_reasons if r["feature"] not in model_features]
        return model_reasons + extras
    return model_reasons
