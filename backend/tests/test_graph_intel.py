"""Unit tests for the graph intelligence client (Phase C2).

These tests never hit the network — we stub ``httpx.AsyncClient`` at the
module level. They verify the mode-gating and failure contracts that keep
the public validation suite passing even when the model-service is down.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import graph_intel


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[dict] = []

    async def post(self, url: str, json: dict) -> _FakeResponse:  # noqa: A002 - match httpx API
        self.calls.append({"url": url, "json": json})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset():
    graph_intel._client = None  # type: ignore[attr-defined]
    yield
    graph_intel._client = None  # type: ignore[attr-defined]


def _run(coro):
    return asyncio.run(coro)


def test_disabled_when_mode_off(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "off")
    monkeypatch.setenv("AFDS_GNN_ENABLED", "1")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://x")
    assert graph_intel.is_enabled() is False


def test_disabled_when_gnn_flag_off(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "shadow")
    monkeypatch.setenv("AFDS_GNN_ENABLED", "false")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://x")
    assert graph_intel.is_enabled() is False


def test_disabled_when_endpoint_missing(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "shadow")
    monkeypatch.setenv("AFDS_GNN_ENABLED", "1")
    monkeypatch.delenv("AFDS_MODEL_ENDPOINT", raising=False)
    assert graph_intel.is_enabled() is False


def test_score_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "off")

    async def go():
        out = await graph_intel.score("alice", {"amount": 10.0})
        assert out is None

    _run(go())


def test_score_swallows_network_errors(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "shadow")
    monkeypatch.setenv("AFDS_GNN_ENABLED", "1")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    fake = _FakeClient(RuntimeError("boom"))

    async def _get_client():
        return fake

    monkeypatch.setattr(graph_intel, "_get_client", _get_client)

    async def go():
        out = await graph_intel.score("alice", {"amount": 10.0})
        assert out is None
        assert fake.calls  # we did try

    _run(go())


def test_score_success_normalises_payload(monkeypatch):
    monkeypatch.setenv("AFDS_MODEL_MODE", "shadow")
    monkeypatch.setenv("AFDS_GNN_ENABLED", "1")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    fake = _FakeClient(
        _FakeResponse(
            {
                "model_name": "gnn",
                "model_version": "v1",
                "score": 0.73,
                "is_anomaly": True,
                "reason_codes": [{"feature": "graph_is_bridge", "value": 1.0, "contribution": 1.0}],
                "latency_ms": 12.3,
            }
        )
    )

    async def _get_client():
        return fake

    monkeypatch.setattr(graph_intel, "_get_client", _get_client)

    async def go():
        out = await graph_intel.score(
            "alice",
            {"amount": 100.0},
            graph_features={"graph_is_bridge": 1.0},
        )
        assert out is not None
        assert out["score"] == pytest.approx(0.73)
        assert out["is_anomaly"] is True
        assert out["model_name"] == "gnn"
        # Confirm the merged feature vector was sent.
        sent = fake.calls[0]["json"]
        assert sent["entity_id"] == "alice"
        assert sent["features"]["amount"] == 100.0
        assert sent["features"]["graph_is_bridge"] == 1.0

    _run(go())
