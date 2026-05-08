"""Unit tests for the model-service ONNX registry (Phase B).

Exercises the safe-fallback path (no ONNX file present) and the lazy
cache / reload semantics. Does NOT require onnxruntime — the registry
is designed to boot with an empty directory on a laptop.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def registry_mod():
    """Load model-service/app/registry.py by path (no installed package)."""
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "model-service" / "app" / "registry.py"
    assert module_path.is_file(), module_path
    spec = importlib.util.spec_from_file_location(
        "afds_tests.registry", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so @dataclass can resolve
    # forward references via cls.__module__.
    sys.modules["afds_tests.registry"] = module
    spec.loader.exec_module(module)
    return module


# ─────────────────────────────────────────────────────────────────
# Empty registry — deterministic fallback
# ─────────────────────────────────────────────────────────────────
def test_empty_registry_describe(registry_mod, tmp_path):
    reg = registry_mod.ModelRegistry(tmp_path)
    out = reg.describe()
    assert out["root"] == str(tmp_path)
    assert out["models"] == {}


def test_get_returns_fallback_model_when_missing(registry_mod, tmp_path):
    reg = registry_mod.ModelRegistry(tmp_path)
    m = reg.get("vae")
    assert m.name == "vae"
    assert m.version == "fallback"
    assert m.session is None
    # The fallback scorer must never raise and must report non-anomalous.
    score, is_anom = m.score({"amount": 5000.0, "velocity_count": 2})
    assert score == 0.0
    assert is_anom is False


def test_get_is_cached(registry_mod, tmp_path):
    reg = registry_mod.ModelRegistry(tmp_path)
    a = reg.get("vae")
    b = reg.get("vae")
    assert a is b  # second call returns the cached Model instance


# ─────────────────────────────────────────────────────────────────
# Version discovery
# ─────────────────────────────────────────────────────────────────
def test_latest_version_picks_lexicographic_max(registry_mod, tmp_path):
    model_dir = tmp_path / "vae"
    (model_dir / "v2026-04-20-0000").mkdir(parents=True)
    (model_dir / "v2026-04-22-0400").mkdir(parents=True)
    (model_dir / "v2026-04-21-1200").mkdir(parents=True)
    reg = registry_mod.ModelRegistry(tmp_path)
    assert reg._latest_version("vae") == "v2026-04-22-0400"


def test_describe_lists_all_versions(registry_mod, tmp_path):
    (tmp_path / "vae" / "v2026-04-22-0400").mkdir(parents=True)
    (tmp_path / "gnn" / "v2026-04-22-0400").mkdir(parents=True)
    reg = registry_mod.ModelRegistry(tmp_path)
    out = reg.describe()
    assert set(out["models"].keys()) == {"vae", "gnn"}
    assert out["models"]["vae"]["latest"] == "v2026-04-22-0400"


# ─────────────────────────────────────────────────────────────────
# Metadata handling (without a real ONNX file)
# ─────────────────────────────────────────────────────────────────
def test_metadata_is_parsed_even_without_onnx(registry_mod, tmp_path):
    version_dir = tmp_path / "vae" / "v2026-04-22-0400"
    version_dir.mkdir(parents=True)
    (version_dir / "metadata.json").write_text(json.dumps({
        "feature_names": ["amount", "velocity"],
        "threshold": 0.42,
        "input_name": "input_1",
    }))
    reg = registry_mod.ModelRegistry(tmp_path)
    m = reg.get("vae")
    assert m.version == "v2026-04-22-0400"
    assert m.feature_names == ["amount", "velocity"]
    assert m.threshold == pytest.approx(0.42)
    assert m.input_name == "input_1"
    # Still no onnxruntime session → fallback scorer.
    assert m.session is None


def test_bad_metadata_does_not_crash(registry_mod, tmp_path):
    version_dir = tmp_path / "vae" / "v2026-04-22-0400"
    version_dir.mkdir(parents=True)
    (version_dir / "metadata.json").write_text("{ this is not JSON")
    reg = registry_mod.ModelRegistry(tmp_path)
    m = reg.get("vae")
    # Defaults preserved.
    assert m.feature_names == []
    assert m.threshold == 0.5


# ─────────────────────────────────────────────────────────────────
# Hot-reload
# ─────────────────────────────────────────────────────────────────
def test_reload_evicts_named_model(registry_mod, tmp_path):
    reg = registry_mod.ModelRegistry(tmp_path)
    m1 = reg.get("vae")
    reg.reload("vae")
    m2 = reg.get("vae")
    assert m1 is not m2  # fresh Model after reload


def test_reload_evicts_all_when_name_is_none(registry_mod, tmp_path):
    reg = registry_mod.ModelRegistry(tmp_path)
    v1 = reg.get("vae")
    g1 = reg.get("gnn")
    reg.reload()
    v2 = reg.get("vae")
    g2 = reg.get("gnn")
    assert v1 is not v2
    assert g1 is not g2
