"""Unit tests for the graph store (Phase C1).

Exercise the memory-fallback path and the feature-store cache wrapper so
they pass on any laptop without Postgres. The PG path is validated via a
monkey-patched ``_postgres_neighborhood`` to keep the tests hermetic.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import feature_store as fs
from app.services import graph_store


@pytest.fixture(autouse=True)
def _reset():
    fs._store = None  # type: ignore[attr-defined]
    yield
    fs._store = None  # type: ignore[attr-defined]


def _run(coro):
    return asyncio.run(coro)


def test_empty_neighborhood_shape():
    empty = graph_store.empty_neighborhood()
    for key in graph_store.feature_keys():
        assert key in empty
        assert empty[key] == 0.0


def test_get_neighborhood_returns_zeroed_when_disabled(monkeypatch):
    monkeypatch.setattr(graph_store, "_ENABLED", False)

    async def go():
        feats = await graph_store.get_neighborhood("alice")
        assert feats == graph_store.empty_neighborhood()

    _run(go())


def test_get_neighborhood_empty_entity_id():
    async def go():
        feats = await graph_store.get_neighborhood("")
        assert feats == graph_store.empty_neighborhood()

    _run(go())


def test_memory_fallback_from_realtime_buffers(monkeypatch):
    """When PG is unavailable, we fall back to in-memory realtime buffers.

    We stub the ``app.routers.realtime`` module to avoid the fastapi dependency
    in this hermetic unit test.
    """
    import sys
    import types
    from collections import defaultdict

    async def _fake_pg(_entity: str):
        return None  # simulate PG miss / unavailable

    monkeypatch.setattr(graph_store, "_postgres_neighborhood", _fake_pg)

    fake_realtime = types.ModuleType("app.routers.realtime")
    fake_realtime._velocity_windows = defaultdict(list)
    fake_realtime._inbound_velocity_windows = defaultdict(list)
    fake_realtime._velocity_windows["bob"].extend(
        [
            {"ts": 0.0, "amount": 100.0, "external_id": "tx-1"},
            {"ts": 0.0, "amount": 200.0, "external_id": "tx-2"},
        ]
    )
    fake_realtime._inbound_velocity_windows["bob"].extend(
        [
            {"ts": 0.0, "sender_id": "alice"},
            {"ts": 0.0, "sender_id": "carol"},
            {"ts": 0.0, "sender_id": "dave"},
        ]
    )
    # Also stub parent packages so the ``from app.routers import realtime``
    # import path resolves without triggering the real module.
    for pkg in ("app", "app.routers"):
        if pkg not in sys.modules:
            sys.modules[pkg] = types.ModuleType(pkg)
    monkeypatch.setitem(sys.modules, "app.routers.realtime", fake_realtime)

    async def go():
        feats = await graph_store.get_neighborhood("bob", use_cache=False)
        assert feats["graph_1hop_degree"] == 5.0  # 2 out + 3 in
        assert feats["graph_1hop_amount_sum"] == 300.0
        assert feats["graph_1hop_amount_max"] == 200.0
        assert feats["graph_present"] == 1.0
        # 3 inbound / 2 outbound → ratio 1.5; bridge heuristic should fire.
        assert feats["graph_is_bridge"] == 1.0

    _run(go())


def test_cache_short_circuits_pg(monkeypatch):
    call_count = {"n": 0}

    async def _fake_pg(_entity: str):
        call_count["n"] += 1
        return {k: 1.0 for k in graph_store.feature_keys()}

    monkeypatch.setattr(graph_store, "_postgres_neighborhood", _fake_pg)

    async def go():
        a = await graph_store.get_neighborhood("cached-entity")
        b = await graph_store.get_neighborhood("cached-entity")
        assert a == b
        # First call hits PG; second call must be served from the feature-store cache.
        assert call_count["n"] == 1

    _run(go())


def test_pg_timeout_degrades_to_memory(monkeypatch):
    async def _slow_pg(_entity: str):
        await asyncio.sleep(0.5)  # way over _FETCH_TIMEOUT_SECONDS
        return {k: 42.0 for k in graph_store.feature_keys()}

    monkeypatch.setattr(graph_store, "_postgres_neighborhood", _slow_pg)
    monkeypatch.setattr(graph_store, "_FETCH_TIMEOUT_SECONDS", 0.01)

    async def go():
        feats = await graph_store.get_neighborhood("timeout-entity", use_cache=False)
        # Should fall through to the memory path, which for an unknown
        # entity yields zeros.
        assert feats == graph_store.empty_neighborhood()

    _run(go())
