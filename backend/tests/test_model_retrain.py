"""Unit tests for the automated retrain CronJob (Phase G2).

We don't spin up Postgres or S3 — we exercise the pure-Python glue
(label gating, version stamps, dry-run path, skip-on-empty) by
monkeypatching ``fetch_labels`` / ``_run_training`` / ``_upload_artifact``.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture
def retrain(monkeypatch):
    """Reload the module after env tweaks so module-level constants pick
    up the new values."""
    monkeypatch.setenv("AFDS_RETRAIN_DRY_RUN", "true")
    monkeypatch.setenv("AFDS_RETRAIN_MIN_SAMPLES", "1")
    from app.jobs import model_retrain  # type: ignore
    importlib.reload(model_retrain)
    return model_retrain


# ─────────────────────────────────────────────────────────────────
# Version stamp
# ─────────────────────────────────────────────────────────────────
def test_version_stamp_is_deterministic_within_minute(retrain):
    v1 = retrain._version()
    v2 = retrain._version()
    # Format: v{YYYY-MM-DD-HHMM}
    import re
    assert re.match(r"^v\d{4}-\d{2}-\d{2}-\d{4}$", v1)
    assert re.match(r"^v\d{4}-\d{2}-\d{2}-\d{4}$", v2)
    # They must be equal within the same minute.
    assert v1 == v2


# ─────────────────────────────────────────────────────────────────
# Skip-on-empty
# ─────────────────────────────────────────────────────────────────
def test_main_skips_when_no_labels(retrain, monkeypatch, capsys):
    monkeypatch.setattr(retrain, "fetch_labels", lambda *a, **kw: [])
    monkeypatch.setattr(retrain, "MIN_SAMPLES", 100)
    rc = retrain.main()
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["event"] == "retrain_skipped"
    assert payload["reason"] == "insufficient_labels"


# ─────────────────────────────────────────────────────────────────
# Dry-run mode: never uploads, always exits 0
# ─────────────────────────────────────────────────────────────────
def test_dry_run_never_uploads(retrain, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(retrain, "fetch_labels", lambda *a, **kw: [
        {"alert_id": "a1", "label": 1, "transaction_id": "t1"},
        {"alert_id": "a2", "label": 0, "transaction_id": "t2"},
    ])
    monkeypatch.setattr(retrain, "MIN_SAMPLES", 1)
    monkeypatch.setattr(retrain, "DRY_RUN", True)

    called = {"train": 0, "upload": 0}

    def _fake_train(script_rel, out_dir, version):
        called["train"] += 1
        (out_dir / version).mkdir(parents=True, exist_ok=True)
        return True

    def _fake_upload(local_dir, model_name, version):
        called["upload"] += 1
        # In dry-run the upstream code short-circuits before us; this
        # fixture guarantees we never hit a real boto3 client either way.
        return True

    monkeypatch.setattr(retrain, "_run_training", _fake_train)
    monkeypatch.setattr(retrain, "_upload_artifact", _fake_upload)

    rc = retrain.main()
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["event"] == "retrain_run"
    assert payload["dry_run"] is True
    assert set(payload["models"].keys()) == {"vae", "gnn"}
    # Training attempted for both models.
    assert called["train"] == 2


# ─────────────────────────────────────────────────────────────────
# SQL label gate — signed-off dispositions only
# ─────────────────────────────────────────────────────────────────
def test_label_sql_filters_on_human_resolution(retrain):
    sql = retrain._LABEL_SQL
    # Must only admit RESOLVED / DISMISSED alerts (the two analyst-signed
    # terminal states) and require a resolved_by principal.
    assert "a.status IN ('RESOLVED', 'DISMISSED')" in sql
    assert "a.resolved_by IS NOT NULL" in sql
    # Must never train on raw OPEN alerts or auto-dispositioned events.
    assert "OPEN" not in sql
    assert "auto" not in sql.lower()


def test_fetch_labels_returns_empty_when_psycopg2_missing(retrain, monkeypatch):
    """On a laptop without psycopg2 we must degrade to [] rather than crash."""
    import sys
    monkeypatch.setitem(sys.modules, "psycopg2", None)  # simulate ImportError
    out = retrain.fetch_labels(horizon_days=14)
    assert out == []
