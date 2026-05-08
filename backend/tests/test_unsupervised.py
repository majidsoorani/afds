"""Unit tests for the VAE unsupervised scorer (Phase D).

These tests never touch onnxruntime inputs beyond a trivially small
session, and the ONNX binary itself is generated on-the-fly only when
the ``onnx`` package is importable (skipped cleanly otherwise).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.services import unsupervised


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Ensure every test re-reads env vars and re-loads the session."""
    monkeypatch.delenv("AFDS_VAE_ENABLED", raising=False)
    monkeypatch.delenv("AFDS_VAE_MODEL_PATH", raising=False)
    monkeypatch.delenv("AFDS_VAE_CALIBRATION_PATH", raising=False)
    unsupervised._reset_for_tests()
    yield
    unsupervised._reset_for_tests()


def test_disabled_by_default_returns_none():
    assert unsupervised.is_enabled() is False
    assert unsupervised.score_features({"amount": 100.0}) is None


def test_enabled_without_model_path_returns_none(monkeypatch):
    monkeypatch.setenv("AFDS_VAE_ENABLED", "true")
    assert unsupervised.is_enabled() is True
    # No AFDS_VAE_MODEL_PATH → graceful None, not an exception.
    assert unsupervised.score_features({"amount": 50.0}) is None


def test_enabled_with_missing_model_file_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("AFDS_VAE_ENABLED", "true")
    monkeypatch.setenv("AFDS_VAE_MODEL_PATH", str(tmp_path / "does_not_exist.onnx"))
    assert unsupervised.score_features({"amount": 50.0}) is None


def _build_identity_onnx(path: Path, n_features: int) -> None:
    onnx = pytest.importorskip("onnx")
    helper = onnx.helper
    TensorProto = onnx.TensorProto

    x = helper.make_tensor_value_info("features", TensorProto.FLOAT, [None, n_features])
    y = helper.make_tensor_value_info("reconstruction", TensorProto.FLOAT, [None, n_features])
    node = helper.make_node("Identity", inputs=["features"], outputs=["reconstruction"])
    graph = helper.make_graph([node], "afds_vae_test", [x], [y])
    model = helper.make_model(
        graph,
        producer_name="afds-tests",
        opset_imports=[helper.make_opsetid("", 17)],
    )
    model.ir_version = 9
    onnx.save(model, str(path))


def test_happy_path_identity_model_returns_zero_error(monkeypatch, tmp_path):
    pytest.importorskip("onnxruntime")
    model_path = tmp_path / "model.onnx"
    _build_identity_onnx(model_path, n_features=len(unsupervised._FEATURE_NAMES))

    calib_path = tmp_path / "calibration.json"
    calib_path.write_text(
        json.dumps(
            {
                "version": "v-test-0",
                # A reasonable calibration: errors clustered near zero, a few
                # outliers. Our identity model reconstructs perfectly, so the
                # percentile should land near 0.
                "reconstruction_errors": [0.01] * 90 + [0.5] * 9 + [2.0],
            }
        )
    )

    monkeypatch.setenv("AFDS_VAE_ENABLED", "true")
    monkeypatch.setenv("AFDS_VAE_MODEL_PATH", str(model_path))
    monkeypatch.setenv("AFDS_VAE_CALIBRATION_PATH", str(calib_path))

    result = unsupervised.score_features(
        {
            "amount": 150.0,
            "velocity_count": 2,
            "hour_of_day": 14,
            "is_weekend": 0,
            "entity_risk": 1.0,
            "ip_risk": 0.0,
            "phone_risk": 0.0,
            "email_risk": 0.0,
            "cop_reason": 0.0,
            "geo_mismatch": 0.0,
        }
    )
    assert result is not None
    assert result["source"] == "onnx.vae"
    assert result["model_version"] == "v-test-0"
    assert result["reconstruction_error"] == pytest.approx(0.0, abs=1e-6)
    # Identity reconstruction → error is 0 → percentile 0 → anomaly_score 0.
    assert result["anomaly_score"] == pytest.approx(0.0, abs=1e-4)
    assert result["is_anomaly"] is False
    assert result["features_used"] == unsupervised._FEATURE_NAMES


def test_anomaly_delegation_falls_back_to_iforest_when_vae_disabled():
    """The realtime scoring path calls anomaly.score_features; when VAE is
    off, it must continue to return the IForest result untouched."""
    from app.services import anomaly

    # VAE disabled (fixture cleared env). IForest path must still work.
    result = anomaly.score_features({"amount": 100.0})
    # pyod may or may not be installed on the laptop; we just assert the
    # contract rather than the specific source.
    assert "anomaly_score" in result
    assert "is_anomaly" in result
    assert result.get("source") != "onnx.vae"
