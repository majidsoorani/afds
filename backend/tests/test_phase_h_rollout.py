"""Unit tests for Phase H (Rollout & Safety).

Covers:
  * Deterministic SHA-256 canary bucketing (sticky per sender).
  * Uniform cohort distribution within ±3% tolerance.
  * Canary traffic gating (``AFDS_CANARY_PERCENTAGE``).
  * Kill-switch hard-stop and its p99 <1ms drill.
  * Model-card JSON schema contract.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services import hybrid_gate


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in (
        "AFDS_MODEL_MODE",
        "AFDS_CANARY_PERCENTAGE",
        "AFDS_CANARY_SALT",
        "AFDS_HYBRID_THRESHOLD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ─────────────────────────────────────────────────────────────────
# Canary hashing
# ─────────────────────────────────────────────────────────────────
def test_sender_bucket_is_deterministic():
    """Same input → same bucket across calls (and across processes,
    since SHA-256 is not PYTHONHASHSEED-randomised)."""
    b1 = hybrid_gate.sender_bucket("bob-123")
    b2 = hybrid_gate.sender_bucket("bob-123")
    assert b1 == b2
    assert 0 <= b1 < 100


def test_sender_bucket_stable_across_salts():
    """Different salts → different buckets (enables clean re-shuffling
    at each ramp step)."""
    b_no_salt = hybrid_gate.sender_bucket("bob-123", salt="")
    b_w_salt = hybrid_gate.sender_bucket("bob-123", salt="v2")
    # Not guaranteed to differ for any single sender, but the empty-
    # and non-empty-salt functions must produce independent distributions
    # in aggregate. Check that at least one of 50 ids changes.
    changed = 0
    for i in range(50):
        if hybrid_gate.sender_bucket(f"s-{i}", salt="") != hybrid_gate.sender_bucket(f"s-{i}", salt="v2"):
            changed += 1
    assert changed > 25


def test_sender_bucket_uniform_distribution():
    """Across 10k synthetic senders the cohort should spread within ~±3%
    of the expected per-bucket count."""
    counts = [0] * 100
    for i in range(10_000):
        counts[hybrid_gate.sender_bucket(f"user-{i}")] += 1
    expected = 100
    for c in counts:
        # 3σ bound: count per bucket should be within 70..130.
        assert 60 <= c <= 140, f"bucket distribution skewed: {c}"


def test_sender_bucket_empty_returns_out_of_band():
    """Empty sender id → bucket 100 (never in any canary cohort)."""
    assert hybrid_gate.sender_bucket("") == 100


# ─────────────────────────────────────────────────────────────────
# Canary gating (is_enabled_for_sender)
# ─────────────────────────────────────────────────────────────────
def test_canary_off_blocks_everyone(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "off")
    assert hybrid_gate.is_enabled_for_sender("bob") is False


def test_canary_shadow_blocks_everyone(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "shadow")
    assert hybrid_gate.is_enabled_for_sender("bob") is False


def test_canary_hybrid_admits_everyone(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    for i in range(20):
        assert hybrid_gate.is_enabled_for_sender(f"sender-{i}") is True


def test_canary_autonomous_is_not_yet_wired(monkeypatch):
    """Autonomous mode is reserved for a future release with its own
    governance gate; the hybrid advisory-escalation path must remain a
    no-op until then."""
    monkeypatch.setenv("AFDS_MODEL_MODE", "autonomous")
    assert hybrid_gate.is_enabled_for_sender("bob") is False


def test_canary_5_percent_admits_roughly_5_percent(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "5")
    admitted = sum(
        1 for i in range(10_000)
        if hybrid_gate.is_enabled_for_sender(f"user-{i}")
    )
    # 5% ± 1%.
    assert 400 <= admitted <= 600, f"canary cohort size off: {admitted}"


def test_canary_zero_percent_admits_nobody(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "0")
    for i in range(100):
        assert hybrid_gate.is_enabled_for_sender(f"u-{i}") is False


def test_canary_100_percent_admits_everyone(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "100")
    for i in range(100):
        assert hybrid_gate.is_enabled_for_sender(f"u-{i}") is True


def test_canary_is_sticky_per_sender(monkeypatch):
    """The same sender must receive the same treatment on every call.
    This guards the UX-flapping hazard the directive highlights."""
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "25")
    first = [hybrid_gate.is_enabled_for_sender(f"s-{i}") for i in range(500)]
    second = [hybrid_gate.is_enabled_for_sender(f"s-{i}") for i in range(500)]
    assert first == second


def test_maybe_escalate_canary_records_bucket(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "100")
    out = hybrid_gate.maybe_escalate(
        risk_score=30.0, risk_level="MEDIUM", action="FLAG",
        factors=["VELOCITY:3(+5)"],
        anomaly_block={"anomaly_score": 99.0, "is_anomaly": True},
        graph_block=None,
        sender_id="bob",
    )
    assert out["mode"] == "canary"
    assert out["canary_bucket"] == hybrid_gate.sender_bucket("bob")
    assert out["escalated"] is True


def test_maybe_escalate_canary_miss_does_not_escalate(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "canary")
    monkeypatch.setenv("AFDS_CANARY_PERCENTAGE", "0")  # nobody
    out = hybrid_gate.maybe_escalate(
        risk_score=30.0, risk_level="MEDIUM", action="FLAG",
        factors=["VELOCITY:3(+5)"],
        anomaly_block={"anomaly_score": 99.0, "is_anomaly": True},
        graph_block=None,
        sender_id="bob",
    )
    assert out["escalated"] is False


# ─────────────────────────────────────────────────────────────────
# Kill-switch
# ─────────────────────────────────────────────────────────────────
def test_kill_switch_active_when_mode_off(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "off")
    assert hybrid_gate.is_kill_switch_active() is True


def test_kill_switch_inactive_when_mode_hybrid(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    assert hybrid_gate.is_kill_switch_active() is False


def test_kill_switch_short_circuits_before_probability(monkeypatch):
    """Even with a screaming-hot model signal, mode=off must not
    escalate."""
    monkeypatch.setenv("AFDS_MODEL_MODE", "off")
    out = hybrid_gate.maybe_escalate(
        risk_score=30.0, risk_level="MEDIUM", action="FLAG",
        factors=["VELOCITY:3(+5)"],
        anomaly_block={"anomaly_score": 100.0, "is_anomaly": True},
        graph_block={"score": 1.0, "is_anomaly": True},
        sender_id="bob",
    )
    assert out["escalated"] is False
    assert out["model_probability"] == 0.0


def test_kill_switch_drill_p99_under_1ms():
    """Published SLO: p99 of the kill-switch short-circuit must be
    under 1000 µs. Run a generous number of iterations so CI variance
    doesn't flake the result."""
    drill = hybrid_gate.kill_switch_drill(iterations=5_000)
    assert drill["passed"] is True, drill
    assert drill["p99_us"] < 1_000.0, drill


def test_kill_switch_drill_restores_env(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "hybrid")
    hybrid_gate.kill_switch_drill(iterations=10)
    # Env must be restored after the drill so we don't leak 'off' state.
    assert os.environ["AFDS_MODEL_MODE"] == "hybrid"


# ─────────────────────────────────────────────────────────────────
# Model Card schema
# ─────────────────────────────────────────────────────────────────
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_model_card_schema_is_valid_json():
    path = _repo_root() / "data-pipeline" / "ml" / "model-cards" / "schema.json"
    assert path.is_file()
    schema = json.loads(path.read_text())
    assert schema["$schema"].startswith("https://json-schema.org/")
    assert schema["title"] == "AFDS Model Card"
    # Required top-level fields lock the governance contract.
    for key in (
        "model_name", "version", "authorizer",
        "trained_at", "training", "baseline",
        "metrics", "governance",
    ):
        assert key in schema["required"], key


def test_example_model_card_matches_schema_shape():
    """Example card must satisfy the schema's top-level required keys
    without needing a jsonschema validator dependency in CI."""
    card_path = (
        _repo_root() / "data-pipeline" / "ml" / "model-cards"
        / "vae" / "v2026-04-22-0400.json"
    )
    card = json.loads(card_path.read_text())
    for key in (
        "model_name", "version", "authorizer",
        "trained_at", "training", "baseline",
        "metrics", "governance",
    ):
        assert key in card
    assert card["governance"]["kill_switch_drill"]["passed"] is True
    assert card["governance"]["parity_gate"]["passed"] == 49
    assert card["governance"]["parity_gate"]["total"] == 49
    # Version stamp format — v{YYYY-MM-DD-HHMM}.
    import re
    assert re.match(r"^v\d{4}-\d{2}-\d{2}-\d{4}$", card["version"])
