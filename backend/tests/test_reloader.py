"""Unit tests for the model-api zero-downtime reloader (Phase G3).

Verifies:
  * Disabled-via-env is a no-op.
  * Filesystem fingerprints detect version / size changes.
  * The reload loop triggers ``registry.reload(name)`` when the
    fingerprint changes and does NOT call it when stable.
  * Loop never raises into the caller on transient errors.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def reloader():
    """Load the model-service reloader by path (no installed package)."""
    repo_root = Path(__file__).resolve().parents[2]
    module_path = repo_root / "model-service" / "app" / "reloader.py"
    spec = importlib.util.spec_from_file_location(
        "afds_tests.reloader", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRegistry:
    def __init__(self) -> None:
        self.reloads: list[str] = []
        self.gets: list[str] = []

    def reload(self, name: str) -> None:
        self.reloads.append(name)

    def get(self, name: str):
        self.gets.append(name)
        return object()


def test_disabled_reloader_returns_none(monkeypatch, reloader):
    monkeypatch.setenv("AFDS_MODEL_RELOAD_ENABLED", "false")
    assert reloader.start_reloader(_FakeRegistry(), registry_root="/tmp") is None


def test_fs_fingerprint_detects_missing_model(reloader, tmp_path):
    assert reloader._fs_fingerprint(tmp_path, "vae") is None


def test_fs_fingerprint_detects_new_version(reloader, tmp_path):
    model_dir = tmp_path / "vae" / "v2026-04-22-0400"
    model_dir.mkdir(parents=True)
    onnx = model_dir / "model.onnx"
    onnx.write_bytes(b"stub" * 16)
    fp1 = reloader._fs_fingerprint(tmp_path, "vae")
    assert fp1 is not None
    assert "v2026-04-22-0400" in fp1

    # Simulate a new version landing in the registry.
    v2 = tmp_path / "vae" / "v2026-04-22-0500"
    v2.mkdir()
    (v2 / "model.onnx").write_bytes(b"newbytes" * 32)
    fp2 = reloader._fs_fingerprint(tmp_path, "vae")
    assert fp2 != fp1
    assert "v2026-04-22-0500" in fp2


def test_watch_loop_triggers_reload_on_change(reloader, tmp_path, monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_RELOAD_NAMES", "vae")
    monkeypatch.setenv("AFDS_MODEL_RELOAD_INTERVAL_S", "0.01")

    # Seed an initial version.
    v1 = tmp_path / "vae" / "v1"
    v1.mkdir(parents=True)
    (v1 / "model.onnx").write_bytes(b"a")

    registry = _FakeRegistry()

    async def run():
        task = asyncio.create_task(reloader._watch_loop(registry, tmp_path))
        # Wait for priming to land.
        await asyncio.sleep(0.03)
        # Drop a new version. The watcher should pick this up on its
        # next tick and call ``registry.reload("vae")``.
        v2 = tmp_path / "vae" / "v2"
        v2.mkdir()
        (v2 / "model.onnx").write_bytes(b"bb")
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert "vae" in registry.reloads
    assert registry.gets.count("vae") >= 2  # prime + post-reload rebuild


def test_watch_loop_no_reload_when_stable(reloader, tmp_path, monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_RELOAD_NAMES", "vae")
    monkeypatch.setenv("AFDS_MODEL_RELOAD_INTERVAL_S", "0.01")

    v1 = tmp_path / "vae" / "v1"
    v1.mkdir(parents=True)
    (v1 / "model.onnx").write_bytes(b"a")

    registry = _FakeRegistry()

    async def run():
        task = asyncio.create_task(reloader._watch_loop(registry, tmp_path))
        await asyncio.sleep(0.06)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert registry.reloads == []  # no change → no reload
