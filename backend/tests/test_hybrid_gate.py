"""Unit tests for the hybrid escalation gate (Phase F3).

Directive checks:
  * Mode=off (default) is a complete no-op.
  * Escalation only fires when all three conditions hold:
      soft_rule (factors present AND 25 ≤ score < 50),
      model_probability ≥ threshold,
      current level is LOW or MEDIUM.
  * risk_score is NEVER modified.
  * Already-HIGH / CRITICAL inputs are idempotent.
"""

from __future__ import annotations

import pytest

from app.services import hybrid_gate


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in ("AFDS_MODEL_MODE", "AFDS_HYBRID_THRESHOLD"):
        monkeypatch.delenv(key, raising=False)
    yield


def _call(**overrides):
    defaults = dict(
        risk_score=30.0,
        risk_level="MEDIUM",
        action="FLAG",
        factors=["VELOCITY:3(+5)", "AMOUNT:1000(+10)"],
        anomaly_block={"anomaly_score": 92.0, "is_anomaly": True},
        graph_block=None,
    )
    defaults.update(overrides)
    return hybrid_gate.maybe_escalate(**defaults)


def test_disabled_mode_is_noop():
    # Default mode=off.
    out = _call()
    assert out["escalated"] is False
    assert out["risk_level"] == "MEDIUM"
    assert out["action"] == "FLAG"


@pytest.mark.parametrize("mode", ["off", "shadow", "autonomous", ""])
def test_non_hybrid_modes_are_noops(monkeypatch, mode):
    monkeypatch.setenv("AFDS_MODEL_MODE", mode)
    out = _call()
    assert out["escalated"] is False


def test_hybrid_happy_path_escalates(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call()
    assert out["escalated"] is True
    assert out["risk_level"] == "HIGH"
    assert out["action"] == "SUSPEND"
    assert out["model_probability"] >= 0.85
    assert "hybrid_escalation" in (out["reason"] or "")


def test_hybrid_below_threshold_does_not_escalate(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(anomaly_block={"anomaly_score": 40.0})  # p=0.40
    assert out["escalated"] is False
    assert out["risk_level"] == "MEDIUM"


def test_hybrid_without_soft_rule_does_not_escalate(monkeypatch):
    """Score below the MEDIUM floor (25) → no soft rule → no escalation."""
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(risk_score=10.0, risk_level="LOW", action="ALLOW", factors=[])
    assert out["escalated"] is False


def test_hybrid_without_any_rule_firing_does_not_escalate(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    # Score meets the floor but ``factors`` is empty → not a rule firing.
    out = _call(factors=[])
    assert out["escalated"] is False


def test_hybrid_already_high_is_idempotent(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(risk_score=60.0, risk_level="HIGH", action="SUSPEND")
    assert out["escalated"] is False
    assert out["risk_level"] == "HIGH"
    assert out["action"] == "SUSPEND"


def test_hybrid_critical_is_idempotent(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(risk_score=80.0, risk_level="CRITICAL", action="BLOCK")
    assert out["escalated"] is False
    assert out["risk_level"] == "CRITICAL"


def test_hybrid_graph_signal_alone_triggers(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(
        anomaly_block=None,
        graph_block={"score": 0.9, "is_anomaly": True},
    )
    assert out["escalated"] is True


def test_hybrid_takes_max_of_signals(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(
        anomaly_block={"anomaly_score": 10.0},  # 0.10
        graph_block={"score": 0.95},             # 0.95
    )
    assert out["escalated"] is True
    assert out["model_probability"] == pytest.approx(0.95, abs=1e-4)


def test_hybrid_custom_threshold(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    monkeypatch.setenv("AFDS_HYBRID_THRESHOLD", "0.99")
    out = _call(anomaly_block={"anomaly_score": 90.0})  # 0.90
    assert out["escalated"] is False
    assert out["threshold"] == pytest.approx(0.99, abs=1e-6)


def test_hybrid_upper_bound_50_excludes_existing_high_scores(monkeypatch):
    """A rule-driven score of exactly 50 is already HIGH by the Flink
    threshold ladder; the gate must not double-escalate."""
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    out = _call(risk_score=50.0, risk_level="HIGH", action="SUSPEND")
    assert out["escalated"] is False


def test_hybrid_never_modifies_risk_score_key():
    """The return payload must not contain a risk_score key — this is
    the canary guarding against accidental score mutation."""
    out = _call()
    assert "risk_score" not in out
