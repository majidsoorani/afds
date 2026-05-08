"""Unit tests for the PSI drift calculation (Phase G1).

Pure-Python, no DB, no Kafka. Covers:
  * Zero-drift case (same distribution).
  * Monotonic growth of PSI as distributions diverge.
  * Classification thresholds.
  * Bin-edge deduplication for skewed inputs.
  * Safe handling of empty inputs and zero-frequency bins.
"""

from __future__ import annotations

import json
import random

import pytest

from app.services import drift


def test_compute_psi_identical_distributions_near_zero():
    rng = random.Random(42)
    values = [rng.gauss(0.5, 0.1) for _ in range(1000)]
    psi, stats = drift.compute_psi(values, list(values))
    assert psi < 0.01
    assert len(stats) >= 2


def test_compute_psi_monotonic_with_distribution_shift():
    rng = random.Random(7)
    baseline = [rng.gauss(0.5, 0.1) for _ in range(2000)]
    slight_shift = [rng.gauss(0.55, 0.1) for _ in range(2000)]
    heavy_shift = [rng.gauss(0.9, 0.2) for _ in range(2000)]
    psi_small, _ = drift.compute_psi(baseline, slight_shift)
    psi_large, _ = drift.compute_psi(baseline, heavy_shift)
    assert psi_small >= 0.0
    assert psi_large > psi_small
    assert psi_large > 0.25  # material drift


def test_compute_psi_empty_inputs_return_zero():
    assert drift.compute_psi([], [1, 2, 3]) == (0.0, [])
    assert drift.compute_psi([1, 2, 3], []) == (0.0, [])


def test_compute_psi_bin_edges_deduplicate_on_skew():
    """Heavily-tied inputs must not crash with zero-width bins."""
    baseline = [0.0] * 95 + [1.0] * 5
    recent = [0.0] * 90 + [1.0] * 10
    psi, stats = drift.compute_psi(baseline, recent, bins=10)
    assert psi >= 0.0
    assert stats  # should still produce at least one bin


def test_classify_thresholds():
    assert drift.classify(0.05) == "stable"
    assert drift.classify(0.15) == "monitor"
    assert drift.classify(0.25) == "alert"
    # Custom threshold
    assert drift.classify(0.15, threshold=0.10) == "alert"


def test_build_report_unavailable_when_no_baseline():
    r = drift.build_report([], [1.0, 2.0], model_name="vae", model_version="v1")
    assert r.status == "unavailable"
    assert r.psi == 0.0
    assert r.n_recent == 2


def test_build_report_stable_with_identical_distributions():
    values = [i / 100 for i in range(100)]
    r = drift.build_report(values, values, model_name="vae", model_version="v1")
    assert r.status == "stable"
    assert r.psi < 0.01


def test_build_report_alert_on_large_shift():
    rng = random.Random(13)
    baseline = [rng.gauss(0.3, 0.05) for _ in range(1000)]
    recent = [rng.gauss(0.8, 0.05) for _ in range(1000)]
    r = drift.build_report(baseline, recent, model_name="vae", model_version="v1")
    assert r.status == "alert"
    assert r.psi >= 0.25


def test_load_baseline_from_calibration_reads_both_keys(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"reconstruction_errors": [0.1, 0.2, 0.3]}))
    assert drift.load_baseline_from_calibration(str(path)) == [0.1, 0.2, 0.3]

    path.write_text(json.dumps({"baseline_scores": [1.0, 2.0]}))
    assert drift.load_baseline_from_calibration(str(path)) == [1.0, 2.0]


def test_load_baseline_from_calibration_graceful_on_missing(tmp_path):
    assert drift.load_baseline_from_calibration(str(tmp_path / "nope.json")) == []


def test_report_to_dict_is_json_serialisable():
    r = drift.build_report(
        [0.1, 0.2, 0.3, 0.4, 0.5] * 20,
        [0.1, 0.2, 0.3, 0.4, 0.5] * 20,
        model_name="vae",
        model_version="v-test",
    )
    payload = r.to_dict()
    assert json.dumps(payload)  # must not raise
    assert payload["model_name"] == "vae"
    assert isinstance(payload["bins"], list)


def test_emit_to_kafka_returns_false_when_unavailable():
    r = drift.build_report([1.0] * 10, [1.0] * 10,
                           model_name="vae", model_version="v1")
    # In the unit-test environment there is no Kafka producer; emit
    # must degrade silently rather than raise.
    assert drift.emit_to_kafka(r) is False


def test_bin_fractions_handle_out_of_range_values():
    edges = [0.0, 0.5, 1.0]
    # Values below min and above max must still be bucketed.
    fracs = drift._bin_fractions([-0.5, 0.25, 0.75, 2.0], edges)
    assert sum(fracs) == pytest.approx(1.0, abs=1e-9)
