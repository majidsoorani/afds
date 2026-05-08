"""Shared pytest fixtures.

Now that every /api/v1/* router requires a Bearer JWT or X-API-Key, we
auto-inject the SERVICE-role API key into the default test clients so
existing tests keep working without sprinkling auth headers everywhere.
"""

from __future__ import annotations

import os

# Make the SERVICE key deterministic for tests.
os.environ.setdefault("AFDS_API_KEY_MCP", "afds-test-service-key")

import pytest
from fastapi.testclient import TestClient as _TestClient
from httpx import AsyncClient as _AsyncClient


_AUTH_HEADERS = {"X-API-Key": os.environ["AFDS_API_KEY_MCP"]}


@pytest.fixture(autouse=True)
def _inject_service_auth(monkeypatch):
    """Patch TestClient and AsyncClient so requests carry the service API key by default."""

    # ── starlette TestClient ────────────────────────────────────────
    orig_testclient_request = _TestClient.request

    def _patched_testclient_request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        for k, v in _AUTH_HEADERS.items():
            headers.setdefault(k, v)
        return orig_testclient_request(self, method, url, headers=headers, **kwargs)

    monkeypatch.setattr(_TestClient, "request", _patched_testclient_request)

    # ── httpx.AsyncClient ───────────────────────────────────────────
    orig_async_request = _AsyncClient.request

    async def _patched_async_request(self, method, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        for k, v in _AUTH_HEADERS.items():
            headers.setdefault(k, v)
        return await orig_async_request(self, method, url, headers=headers, **kwargs)

    monkeypatch.setattr(_AsyncClient, "request", _patched_async_request)

    yield


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Service-role auth header for tests that build requests manually."""
    return dict(_AUTH_HEADERS)
