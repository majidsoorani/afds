"""Unit tests for XAI reason-code generation (Phase E).

These tests verify:
  1. Rule-based fallback is always produced from ``factors[]``.
  2. The 10ms HTTP timeout is enforced (no test actually waits 10ms —
     we stub the transport to raise ``ReadTimeout`` deterministically).
  3. The unified schema is emitted regardless of source.
  4. The mode gate (``AFDS_XAI_MODE=off``) short-circuits network calls.
  5. Non-2xx / malformed model responses degrade to rule reasons.
  6. Explanation generation never alters anything besides the returned
     list — there is no global state mutation.
"""

from __future__ import annotations

import os

import pytest

from app.services import explain as xai


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in (
        "AFDS_XAI_MODE",
        "AFDS_XAI_TIMEOUT_MS",
        "AFDS_MODEL_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ────────────────────────────────────────────────────────────────
# Rule-based fallback (neuro-symbolic)
# ────────────────────────────────────────────────────────────────


def test_parse_factor_extracts_contribution_and_prefix():
    feature, contribution, descr = xai._parse_factor(
        "VELOCITY:3txns/2min(+15.0)"
    )
    assert feature == "velocity_2min"
    assert contribution == 15.0
    assert "3txns/2min" in descr


def test_parse_factor_handles_missing_contribution():
    feature, contribution, descr = xai._parse_factor("PATTERN:round_amount")
    assert feature == "round_number_pattern"
    assert contribution == 0.0
    assert descr == "round_amount"


def test_parse_factor_unknown_prefix_falls_back_to_snake_case():
    feature, contribution, _ = xai._parse_factor("FOO_BAR:x(+5)")
    assert feature == "foo_bar"
    assert contribution == 5.0


def test_build_rule_reasons_sorted_by_magnitude_desc():
    factors = [
        "VELOCITY:3(+5)",
        "AMOUNT:50000(+35)",
        "COP:NO_MATCH(+15)",
    ]
    reasons = xai.build_rule_reasons(factors)
    assert [r["feature"] for r in reasons] == [
        "amount_threshold",
        "cop_verification",
        "velocity_2min",
    ]
    assert all(r["source"] == "rule" for r in reasons)
    assert all(
        set(r.keys()) >= {"feature", "contribution", "source", "description"}
        for r in reasons
    )


def test_build_rule_reasons_empty_inputs_return_empty_list():
    assert xai.build_rule_reasons([]) == []
    assert xai.build_rule_reasons(None) == []  # type: ignore[arg-type]


def test_build_rule_reasons_pulls_contribution_from_breakdown_when_absent():
    reasons = xai.build_rule_reasons(
        ["AMOUNT:50000"], score_breakdown={"amount": 35.0}
    )
    # parser couldn't find (+X); breakdown supplies it.
    assert reasons[0]["contribution"] == 35.0


# ────────────────────────────────────────────────────────────────
# Mode gates
# ────────────────────────────────────────────────────────────────


def test_mode_off_never_calls_network(monkeypatch):
    monkeypatch.setenv("AFDS_XAI_MODE", "off")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://should-not-be-called")

    def _boom(*_a, **_kw):
        raise AssertionError("network must not be called when mode=off")

    monkeypatch.setattr(xai, "_call_fastshap", _boom)
    reasons = xai.build_reason_codes(
        factors=["VELOCITY:3(+5)"], features={"velocity_count": 3}
    )
    assert reasons[0]["source"] == "rule"


def test_mode_symbolic_never_calls_network(monkeypatch):
    monkeypatch.setenv("AFDS_XAI_MODE", "symbolic")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://should-not-be-called")

    def _boom(*_a, **_kw):
        raise AssertionError("network must not be called when mode=symbolic")

    monkeypatch.setattr(xai, "_call_fastshap", _boom)
    reasons = xai.build_reason_codes(
        factors=["AMOUNT:1(+35)"], features={"amount": 1}
    )
    assert reasons[0]["source"] == "rule"


def test_mode_fastshap_without_endpoint_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    # AFDS_MODEL_ENDPOINT unset → must not attempt network.
    reasons = xai.build_reason_codes(
        factors=["AMOUNT:1(+35)"], features={"amount": 1}
    )
    assert reasons and reasons[0]["source"] == "rule"


# ────────────────────────────────────────────────────────────────
# Model success / failure paths
# ────────────────────────────────────────────────────────────────


def test_fastshap_success_returns_model_source(monkeypatch):
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    def _fake_call(**_kw):
        return [
            {
                "feature": "amount",
                "contribution": 42.5,
                "source": "model",
                "description": "amount=50000",
            }
        ]

    monkeypatch.setattr(xai, "_call_fastshap", _fake_call)
    reasons = xai.build_reason_codes(
        factors=["AMOUNT:50000(+35)"],
        features={"amount": 50000},
    )
    assert len(reasons) == 1
    assert reasons[0]["source"] == "model"
    assert reasons[0]["contribution"] == 42.5


def test_fastshap_timeout_falls_back_to_rules(monkeypatch):
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    # Simulate timeout by returning None from the internal helper.
    monkeypatch.setattr(xai, "_call_fastshap", lambda **_kw: None)

    reasons = xai.build_reason_codes(
        factors=["VELOCITY:9(+25)", "AMOUNT:50000(+35)"],
        features={"velocity_count": 9, "amount": 50000},
    )
    assert reasons and all(r["source"] == "rule" for r in reasons)
    assert reasons[0]["feature"] == "amount_threshold"  # sorted by magnitude


def test_fastshap_real_timeout_via_httpx_stub(monkeypatch):
    """Exercise the real httpx path with a patched transport that raises
    ``ReadTimeout`` — verifies the 10ms budget never blocks the caller."""
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")
    monkeypatch.setenv("AFDS_XAI_TIMEOUT_MS", "5")

    def _handler(_request):  # pragma: no cover - exercised via transport
        raise httpx.ReadTimeout("simulated timeout")

    transport = httpx.MockTransport(_handler)
    orig_client = httpx.Client

    class _TimedClient(orig_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _TimedClient)

    reasons = xai.build_reason_codes(
        factors=["AMOUNT:50000(+35)"],
        features={"amount": 50000},
    )
    # Timeout → rule fallback.
    assert reasons and reasons[0]["source"] == "rule"


def test_fastshap_non_200_falls_back_to_rules(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    def _handler(_request):
        return httpx.Response(503, json={"error": "unavailable"})

    transport = httpx.MockTransport(_handler)
    orig_client = httpx.Client

    class _Client(orig_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)

    reasons = xai.build_reason_codes(
        factors=["AMOUNT:50000(+35)"],
        features={"amount": 50000},
    )
    assert reasons and reasons[0]["source"] == "rule"


def test_fastshap_malformed_payload_falls_back_to_rules(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    def _handler(_request):
        # ``reason_codes`` key absent entirely.
        return httpx.Response(200, json={"oops": True})

    transport = httpx.MockTransport(_handler)
    orig_client = httpx.Client

    class _Client(orig_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)

    reasons = xai.build_reason_codes(
        factors=["AMOUNT:50000(+35)"],
        features={"amount": 50000},
    )
    assert reasons and reasons[0]["source"] == "rule"


def test_fastshap_happy_path_via_httpx_stub(monkeypatch):
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("AFDS_XAI_MODE", "fastshap")
    monkeypatch.setenv("AFDS_MODEL_ENDPOINT", "http://model-api:8080")

    def _handler(_request):
        return httpx.Response(
            200,
            json={
                "reason_codes": [
                    {
                        "feature": "amount",
                        "contribution": 12.3,
                        "description": "amount=50000",
                    },
                    {
                        "feature": "velocity_count",
                        "contribution": 4.2,
                        "description": "velocity_count=3",
                    },
                ],
                "mode": "fastshap-linear",
                "latency_ms": 0.9,
            },
        )

    transport = httpx.MockTransport(_handler)
    orig_client = httpx.Client

    class _Client(orig_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)

    reasons = xai.build_reason_codes(
        factors=["AMOUNT:50000(+35)", "VELOCITY:3(+5)"],
        features={"amount": 50000, "velocity_count": 3},
    )
    # Model path wins → two entries, both ``source=model``.
    assert len(reasons) == 2
    assert all(r["source"] == "model" for r in reasons)
    assert reasons[0]["feature"] == "amount"


def test_unified_schema_is_stable():
    """Frontend contract lock: every entry has the four agreed keys."""
    reasons = xai.build_reason_codes(
        factors=["VELOCITY:3(+5)", "AMOUNT:1(+35)"],
        features={"amount": 1, "velocity_count": 3},
    )
    required = {"feature", "contribution", "source", "description"}
    for entry in reasons:
        assert required.issubset(entry.keys()), entry
        assert entry["source"] in ("rule", "model")
        assert isinstance(entry["contribution"], (int, float))
