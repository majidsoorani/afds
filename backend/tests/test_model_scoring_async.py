"""Unit tests for the Flink async-I/O operator helpers (Phase F1).

We only exercise the pure, PyFlink-independent helpers here — the
AsyncFunction itself requires a JVM toolchain and is exercised via the
Flink integration suite in CI.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def op():
    """Load the Flink job module directly by path so we don't need the
    stream-processor directory on PYTHONPATH."""
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "stream-processor" / "flink-jobs" / "model_scoring_async.py"
    spec = importlib.util.spec_from_file_location(
        "afds_tests.model_scoring_async", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_record_accepts_dict_payload(op):
    out = op._parse_record(json.dumps({"amount": 100, "sender_id": "bob"}))
    assert out == {"amount": 100, "sender_id": "bob"}


def test_parse_record_rejects_non_dict_payload(op):
    assert op._parse_record(json.dumps([1, 2, 3])) is None


def test_parse_record_handles_invalid_json(op):
    assert op._parse_record("not-json") is None


def test_parse_record_accepts_bytes(op):
    out = op._parse_record(b'{"amount": 42}')
    assert out == {"amount": 42}


def test_build_features_projects_known_keys_only(op):
    feats = op._build_features(
        {
            "amount": 150.5,
            "velocity_count": 3,
            "unrelated_field": "ignored",
            "hour_of_day": "14",  # stringified numeric — must coerce
        }
    )
    assert feats["amount"] == 150.5
    assert feats["velocity_count"] == 3.0
    assert feats["hour_of_day"] == 14.0
    assert "unrelated_field" not in feats
    # All contract keys present even when missing from input.
    for key in (
        "amount",
        "velocity_count",
        "hour_of_day",
        "is_weekend",
        "entity_risk",
        "ip_risk",
        "phone_risk",
        "email_risk",
        "cop_reason",
        "geo_mismatch",
    ):
        assert key in feats


def test_build_features_missing_keys_default_to_zero(op):
    feats = op._build_features({})
    assert all(v == 0.0 for v in feats.values())


def test_build_features_non_numeric_coerces_to_default(op):
    feats = op._build_features({"amount": "junk"})
    assert feats["amount"] == 0.0


def test_emit_preserves_joiner_keys_and_source(op):
    original = json.dumps(
        {
            "transaction_id": "abc-123",
            "external_id": "ext-1",
            "sender_id": "bob",
            "event_time": "2026-04-22T10:00:00Z",
        }
    )
    payload = {
        "model_score": 0.73,
        "is_anomaly": True,
        "reason_codes": [{"feature": "amount", "contribution": 0.4}],
        "model_version": "v-test",
        "latency_ms": 12.5,
    }
    out = json.loads(op._emit(original, payload, source="model"))
    assert out["transaction_id"] == "abc-123"
    assert out["sender_id"] == "bob"
    assert out["event_time"] == "2026-04-22T10:00:00Z"
    assert out["model_score"] == 0.73
    assert out["is_anomaly"] is True
    assert out["source"] == "model"
    assert out["model_version"] == "v-test"


def test_emit_safe_default_shape(op):
    out = json.loads(
        op._emit(
            json.dumps({"transaction_id": "tx-9"}),
            op.SAFE_DEFAULT_SCORE,
            source="timeout",
        )
    )
    assert out["source"] == "timeout"
    assert out["model_score"] == 0.0
    assert out["is_anomaly"] is False
    assert out["reason_codes"] == []


def test_emit_handles_bad_original_payload_gracefully(op):
    out = json.loads(op._emit("not-json", op.SAFE_DEFAULT_SCORE, source="parse_error"))
    # No parent keys survive, but the emitted doc is still valid and sinkable.
    assert out["source"] == "parse_error"
    assert out["model_score"] == 0.0


def test_safe_default_constants_are_numeric(op):
    assert op.DEFAULT_TIMEOUT_MS == 80
    assert op.DEFAULT_CAPACITY >= 100  # throughput floor
    assert op.DEFAULT_PARALLELISM >= 1
