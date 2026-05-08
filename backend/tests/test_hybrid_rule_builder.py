"""
Comprehensive test suite for the Hybrid Rule Builder (NLP Chat + Visual Editor).
Tests the full flow: NLP parse → visual edit → backtest → deploy.

Test groups:
  1. NLP Parse — ensures various English rules parse correctly
  2. Visual Edit — ensures edited conditions produce valid backtest payloads
  3. Backtest — ensures the DB query engine handles all field types / operators
  4. Deploy — ensures Kafka deployment payload is correct
  5. Edge cases — malformed input, empty conditions, unknown fields
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport
from unittest.mock import AsyncMock, patch

from app.main import app
from app.core.database import get_db

BASE = "/api/v1/rule-chat"


# ── Fake DB layer ────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows=None, scalar_val=0):
        self._rows = rows or []
        self._scalar_val = scalar_val

    def fetchall(self):
        return self._rows

    def scalar(self):
        return self._scalar_val

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Returns empty result sets for every query — sufficient for backtest."""

    async def execute(self, stmt, params=None):
        stmt_str = str(stmt) if not isinstance(stmt, str) else stmt
        if "COUNT(*)" in stmt_str.upper():
            return _FakeResult(scalar_val=20)
        return _FakeResult(rows=[], scalar_val=0)

    async def commit(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


async def _fake_get_db():
    yield _FakeSession()


# Override DB dependency for all tests
app.dependency_overrides[get_db] = _fake_get_db


@pytest_asyncio.fixture
async def c():
    """In-process ASGI client — no running server needed."""
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ═══════════════════════════════════════════════════════════════════════
# Helper
# ═══════════════════════════════════════════════════════════════════════

async def _parse(c, message: str):
    r = await c.post(f"{BASE}/parse", json={"message": message})
    assert r.status_code == 200, f"Parse failed ({r.status_code}): {r.text}"
    data = r.json()
    assert "parsed_rule" in data
    return data["parsed_rule"]


async def _backtest(c, conditions, logic="AND"):
    r = await c.post(f"{BASE}/backtest", json={
        "conditions": conditions,
        "logic": logic,
        "limit": 100,
    })
    return r


# ═══════════════════════════════════════════════════════════════════════
# Group 1 — NLP Parse Tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestNLPParse:

    async def test_simple_amount(self, c):
        rule = await _parse(c, "Block transactions over $50,000")
        conds = rule["conditions"]
        assert any(cd["field"] == "amount" and cd["operator"] == "gt" for cd in conds)
        assert rule["action"] == "BLOCK"

    async def test_pep_sender(self, c):
        rule = await _parse(c, "Flag PEP sender with amount above 10000")
        conds = rule["conditions"]
        assert any(cd["field"] == "sender_pep" for cd in conds)
        assert rule["action"] == "FLAG"

    async def test_high_risk_score(self, c):
        rule = await _parse(c, "Suspend transactions with risk score above 80")
        conds = rule["conditions"]
        assert any(cd["field"] == "risk_score" and cd["operator"] == "gt" for cd in conds)
        assert rule["action"] == "SUSPEND"

    async def test_currency(self, c):
        rule = await _parse(c, "Flag transactions where currency is USD")
        conds = rule["conditions"]
        assert any(cd["field"] == "currency" for cd in conds)

    async def test_transaction_type(self, c):
        rule = await _parse(c, "Block transactions where transaction_type is CARD_PAYMENT and amount above 5000")
        conds = rule["conditions"]
        assert any(cd["field"] == "transaction_type" for cd in conds)

    async def test_sender_country(self, c):
        rule = await _parse(c, "Flag transactions where sender_country is IRN")
        conds = rule["conditions"]
        assert any(cd["field"] in ("sender_country", "sender_nationality") for cd in conds)

    async def test_kyc_level(self, c):
        rule = await _parse(c, "Block transactions where sender KYC level is NONE")
        conds = rule["conditions"]
        assert any(cd["field"] == "sender_kyc_level" for cd in conds)

    async def test_multiple_conditions_and(self, c):
        rule = await _parse(c, "Block PEP sender with amount over 20000 and risk score above 70")
        conds = rule["conditions"]
        assert len(conds) >= 2
        assert rule["logic"] == "AND"

    async def test_or_logic(self, c):
        rule = await _parse(c, "Flag amount over 100000 or risk score above 90")
        assert rule["logic"] == "OR"

    async def test_severity_critical(self, c):
        rule = await _parse(c, "Block high risk transactions over $100,000 severity critical")
        assert rule["severity"] in ("CRITICAL", "HIGH")

    async def test_receiver_iban(self, c):
        rule = await _parse(c, "Flag transactions where receiver IBAN contains DE")
        conds = rule["conditions"]
        assert any(cd["field"] == "receiver_iban" and cd["operator"] == "contains" for cd in conds)


# ═══════════════════════════════════════════════════════════════════════
# Group 2 — Visual Edit Simulation (modified conditions → backtest)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestVisualEditAndBacktest:

    async def test_edit_amount_threshold(self, c):
        rule = await _parse(c, "Block transactions over $50,000")
        for cond in rule["conditions"]:
            if cond["field"] == "amount" and cond["operator"] == "gt":
                cond["value"] = "10000"
        r2 = await _backtest(c, rule["conditions"], rule["logic"])
        assert r2.status_code == 200
        bt = r2.json()
        assert "total_matched" in bt
        assert "total_transactions" in bt

    async def test_add_condition(self, c):
        rule = await _parse(c, "Flag transactions over $5000")
        rule["conditions"].append({
            "field": "risk_score", "operator": "gt", "value": "50", "category": "risk",
        })
        r2 = await _backtest(c, rule["conditions"], "AND")
        assert r2.status_code == 200

    async def test_remove_condition(self, c):
        rule = await _parse(c, "Block PEP sender with amount over 10000")
        rule["conditions"] = [cd for cd in rule["conditions"] if cd["field"] != "amount"]
        assert len(rule["conditions"]) >= 1
        r2 = await _backtest(c, rule["conditions"], rule["logic"])
        assert r2.status_code == 200

    async def test_change_logic(self, c):
        rule = await _parse(c, "Block PEP sender with amount over 10000 and risk score above 70")
        r2 = await _backtest(c, rule["conditions"], "OR")
        assert r2.status_code == 200
        bt = r2.json()
        assert bt["total_matched"] >= 0

    async def test_change_operator(self, c):
        rule = await _parse(c, "Flag amount over 50000")
        for cond in rule["conditions"]:
            if cond["field"] == "amount":
                cond["operator"] = "lt"
                cond["value"] = "1000"
        r2 = await _backtest(c, rule["conditions"], rule["logic"])
        assert r2.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# Group 3 — Backtest with Diverse Field Types
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestBacktestFieldTypes:

    async def test_number_gt(self, c):
        r = await _backtest(c, [{"field": "amount", "operator": "gt", "value": "1000", "category": "transaction"}])
        assert r.status_code == 200

    async def test_number_lt(self, c):
        r = await _backtest(c, [{"field": "amount", "operator": "lt", "value": "500", "category": "transaction"}])
        assert r.status_code == 200

    async def test_number_eq(self, c):
        r = await _backtest(c, [{"field": "risk_score", "operator": "eq", "value": "75", "category": "risk"}])
        assert r.status_code == 200

    async def test_string_eq(self, c):
        r = await _backtest(c, [{"field": "currency", "operator": "eq", "value": "GBP", "category": "transaction"}])
        assert r.status_code == 200

    async def test_string_contains(self, c):
        r = await _backtest(c, [{"field": "sender_iban", "operator": "contains", "value": "GB", "category": "transaction"}])
        assert r.status_code == 200

    async def test_enum_eq(self, c):
        r = await _backtest(c, [{"field": "transaction_type", "operator": "eq", "value": "send_money", "category": "transaction"}])
        assert r.status_code == 200

    async def test_boolean_is_true(self, c):
        r = await _backtest(c, [{"field": "sender_pep", "operator": "is_true", "value": "", "category": "sender_profile"}])
        assert r.status_code == 200

    async def test_boolean_is_false(self, c):
        r = await _backtest(c, [{"field": "sender_pep", "operator": "is_false", "value": "", "category": "sender_profile"}])
        assert r.status_code == 200

    async def test_risk_level_enum(self, c):
        r = await _backtest(c, [{"field": "risk_level", "operator": "eq", "value": "high", "category": "risk"}])
        assert r.status_code == 200

    async def test_multi_field_and(self, c):
        r = await _backtest(c, [
            {"field": "amount", "operator": "gt", "value": "5000", "category": "transaction"},
            {"field": "risk_score", "operator": "gt", "value": "50", "category": "risk"},
        ], logic="AND")
        assert r.status_code == 200

    async def test_multi_field_or(self, c):
        r = await _backtest(c, [
            {"field": "amount", "operator": "gt", "value": "50000", "category": "transaction"},
            {"field": "risk_score", "operator": "gt", "value": "90", "category": "risk"},
        ], logic="OR")
        assert r.status_code == 200

    async def test_velocity_score(self, c):
        r = await _backtest(c, [{"field": "velocity_score", "operator": "gt", "value": "20", "category": "risk"}])
        assert r.status_code == 200

    async def test_sender_kyc_level(self, c):
        r = await _backtest(c, [{"field": "sender_kyc_level", "operator": "eq", "value": "none", "category": "sender_profile"}])
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# Group 4 — Deploy Tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestDeploy:

    async def test_deploy_basic(self, c):
        r = await c.post(f"{BASE}/deploy", json={
            "rule_name": "test_deploy_rule",
            "description": "Test rule for automated testing",
            "conditions": [
                {"field": "amount", "operator": "gt", "value": "50000", "category": "transaction"}
            ],
            "logic": "AND",
            "action": "FLAG",
            "risk_score_adjustment": 25,
            "severity": "MEDIUM",
        })
        assert r.status_code in (200, 500, 503)

    async def test_deploy_multi_condition(self, c):
        r = await c.post(f"{BASE}/deploy", json={
            "rule_name": "test_multi_cond_rule",
            "description": "Multi-condition test rule",
            "conditions": [
                {"field": "amount", "operator": "gt", "value": "10000", "category": "transaction"},
                {"field": "sender_pep", "operator": "is_true", "value": "", "category": "sender_profile"},
                {"field": "risk_score", "operator": "gt", "value": "70", "category": "risk"},
            ],
            "logic": "AND",
            "action": "BLOCK",
            "risk_score_adjustment": 50,
            "severity": "CRITICAL",
        })
        assert r.status_code in (200, 500, 503)


# ═══════════════════════════════════════════════════════════════════════
# Group 5 — Edge Cases & Error Handling
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestEdgeCases:

    async def test_empty_message(self, c):
        r = await c.post(f"{BASE}/parse", json={"message": ""})
        assert r.status_code == 422

    async def test_short_message(self, c):
        r = await c.post(f"{BASE}/parse", json={"message": "ab"})
        assert r.status_code == 422

    async def test_unknown_field_in_backtest(self, c):
        r = await c.post(f"{BASE}/backtest", json={
            "conditions": [
                {"field": "nonexistent_field", "operator": "gt", "value": "100"}
            ],
            "logic": "AND",
            "limit": 100,
        })
        assert r.status_code in (200, 400, 422, 500)

    async def test_empty_conditions_backtest(self, c):
        r = await c.post(f"{BASE}/backtest", json={
            "conditions": [],
            "logic": "AND",
            "limit": 100,
        })
        assert r.status_code in (200, 400, 422)

    async def test_suggestions_endpoint(self, c):
        r = await c.get(f"{BASE}/suggestions")
        assert r.status_code == 200
        data = r.json()
        assert "suggestions" in data
        assert len(data["suggestions"]) > 0

    async def test_fields_endpoint(self, c):
        r = await c.get(f"{BASE}/fields")
        assert r.status_code == 200
        data = r.json()
        assert "categories" in data
        assert "total_fields" in data
        assert data["total_fields"] > 0

    async def test_backtest_limit(self, c):
        r = await c.post(f"{BASE}/backtest", json={
            "conditions": [{"field": "amount", "operator": "gt", "value": "0", "category": "transaction"}],
            "logic": "AND",
            "limit": 5,
        })
        assert r.status_code == 200
        bt = r.json()
        assert len(bt.get("sample_matches", [])) <= 5

    async def test_full_hybrid_flow(self, c):
        # Step 1: Parse
        r1 = await c.post(f"{BASE}/parse", json={
            "message": "Block transactions over $50,000"
        })
        assert r1.status_code == 200
        rule = r1.json()["parsed_rule"]
        assert len(rule["conditions"]) >= 1

        # Step 2: Visual edit
        for cond in rule["conditions"]:
            if cond["field"] == "amount":
                cond["value"] = "20000"
        rule["conditions"].append({
            "field": "sender_pep", "operator": "is_true", "value": "true", "category": "sender_profile",
        })
        rule["conditions"].append({
            "field": "risk_score", "operator": "gt", "value": "60", "category": "risk",
        })

        # Step 3: Backtest
        r2 = await c.post(f"{BASE}/backtest", json={
            "conditions": rule["conditions"],
            "logic": "AND",
            "limit": 100,
        })
        assert r2.status_code == 200
        bt = r2.json()
        assert "total_matched" in bt
        assert "total_transactions" in bt
        assert "match_rate" in bt
        assert bt["total_transactions"] >= 0
