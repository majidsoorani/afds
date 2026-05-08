"""Unit tests for the online feature store (Phase A1).

We only exercise the memory-fallback path here so these tests never require
a running Redis — they must pass in CI and on a bare laptop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import feature_store as fs


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Ensure every test sees a fresh FeatureStore singleton."""
    fs._store = None  # type: ignore[attr-defined]
    yield
    fs._store = None  # type: ignore[attr-defined]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_memory_put_get_roundtrip():
    async def go():
        store = fs.FeatureStore(redis_url="")  # force memory
        await store.connect()
        assert store.backend == "memory"
        await store.put("sender", "velocity-demo", {"velocity_count": 3, "amount": 10.5})
        got = await store.get("sender", "velocity-demo")
        assert got == {"velocity_count": 3, "amount": 10.5}

    _run(go())


def test_memory_miss_returns_none():
    async def go():
        store = fs.FeatureStore(redis_url="")
        await store.connect()
        assert await store.get("sender", "does-not-exist") is None

    _run(go())


def test_memory_ttl_expiry():
    async def go():
        store = fs.FeatureStore(redis_url="")
        await store.connect()
        await store.put("sender", "e", {"x": 1}, ttl=1)
        assert await store.get("sender", "e") == {"x": 1}
        # Force expiry by rewinding the stored deadline
        key = fs._compose_key("sender", "e", None)
        expires_at, value = store._memory._data[key]  # type: ignore[attr-defined]
        store._memory._data[key] = (time.time() - 0.1, value)  # type: ignore[attr-defined]
        assert await store.get("sender", "e") is None

    _run(go())


def test_memory_lru_eviction():
    async def go():
        store = fs.FeatureStore(redis_url="", memory_max=3)
        await store.connect()
        for i in range(5):
            await store.put("sender", f"k{i}", {"i": i})
        # Oldest two should be evicted (k0, k1)
        assert await store.get("sender", "k0") is None
        assert await store.get("sender", "k1") is None
        assert (await store.get("sender", "k4")) == {"i": 4}

    _run(go())


def test_window_scoped_keys_are_independent():
    async def go():
        store = fs.FeatureStore(redis_url="")
        await store.connect()
        await store.put("sender", "bob", {"n": 1}, window="2min")
        await store.put("sender", "bob", {"n": 99}, window="24h")
        assert (await store.get("sender", "bob", window="2min")) == {"n": 1}
        assert (await store.get("sender", "bob", window="24h")) == {"n": 99}

    _run(go())


def test_delete_removes_from_memory():
    async def go():
        store = fs.FeatureStore(redis_url="")
        await store.connect()
        await store.put("sender", "bob", {"n": 1})
        await store.delete("sender", "bob")
        assert await store.get("sender", "bob") is None

    _run(go())


def test_health_reports_memory_backend_when_unconfigured():
    async def go():
        store = fs.FeatureStore(redis_url="")
        await store.connect()
        health = await store.health()
        assert health["backend"] == "memory"
        assert health["redis_configured"] is False

    _run(go())


def test_get_feature_store_returns_singleton():
    async def go():
        a = await fs.get_feature_store()
        b = await fs.get_feature_store()
        assert a is b

    _run(go())


def test_compose_key_sanitises_colons():
    key = fs._compose_key("scope:x", "entity:y", "window:z")
    # Exactly 4 colons: prefix + 3 parts
    # afds:feat : scope_x : entity_y : window_z  →  4 colons in prefix+separators
    assert key.count(":") == 4
    assert "scope:x" not in key
    assert "scope_x" in key
