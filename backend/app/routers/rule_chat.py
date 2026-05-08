"""
Rule Chat — Natural language → structured detection rule conversion,
backtesting against historical transactions, and deployment to Flink via Kafka.

Flow: English text → parse → test on DB → approve → Kafka `detection-rules` → Flink

Field catalog spans: Transaction, User Profile, Card, Risk Score, Velocity, Sanctions.
"""

import json
import logging
import re
import uuid
from asyncio import wait_for, TimeoutError as AsyncTimeoutError
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.kafka import get_kafka_producer
from app.routers.rules import register_rule, fetch_rule, apply_rule_updates
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rule-chat", tags=["rule-chat"])


# ── Schemas ──────────────────────────────────────────────────────────

class ParseRequest(BaseModel):
    message: str = Field(..., min_length=3, max_length=2000, description="Rule in plain English")


class ParsedRule(BaseModel):
    rule_name: str
    description: str
    conditions: list[dict]  # [{field, operator, value, category}]
    logic: str = "AND"
    action: str = "FLAG"
    risk_score_adjustment: int = 30
    severity: str = "HIGH"


class BacktestRequest(BaseModel):
    conditions: list[dict]
    logic: str = "AND"
    limit: int = Field(default=500, le=5000)


class DeployRequest(BaseModel):
    rule_name: str
    description: str
    conditions: list[dict]
    logic: str = "AND"
    action: str = "FLAG"
    risk_score_adjustment: int = 30
    severity: str = "HIGH"


class EditRequest(BaseModel):
    rule_id: str = Field(..., description="ID of the rule to edit")
    message: str = Field(..., min_length=3, max_length=2000, description="Edit instruction in English")


class DirectEditRequest(BaseModel):
    rule_id: str
    description: str | None = None
    conditions: list[dict] | None = None
    logic: str | None = None
    action: str | None = None
    risk_score_adjustment: int | None = None
    severity: str | None = None


# ═══════════════════════════════════════════════════════════════════
# FIELD CATALOG — Every field the user can build rules on
# ═══════════════════════════════════════════════════════════════════

FIELD_CATALOG = {
    # ── Transaction fields ──
    "amount":               {"col": "t.amount",              "type": "number",  "category": "transaction", "label": "Transaction Amount",      "desc": "Transaction value"},
    "currency":             {"col": "t.currency",            "type": "string",  "category": "transaction", "label": "Currency",                "desc": "ISO currency code (GBP, USD, EUR…)"},
    "sender_id":            {"col": "t.sender_id",           "type": "string",  "category": "transaction", "label": "Sender ID",               "desc": "Sender account/user identifier"},
    "receiver_id":          {"col": "t.receiver_id",         "type": "string",  "category": "transaction", "label": "Receiver ID",             "desc": "Receiver account/user identifier"},
    "sender_iban":          {"col": "t.sender_iban",         "type": "string",  "category": "transaction", "label": "Sender IBAN",             "desc": "Sender IBAN"},
    "receiver_iban":        {"col": "t.receiver_iban",       "type": "string",  "category": "transaction", "label": "Receiver IBAN",           "desc": "Receiver IBAN"},
    "transaction_type":     {"col": "t.transaction_type",    "type": "enum",    "category": "transaction", "label": "Transaction Type",        "desc": "SEND_MONEY, ADD_MONEY, DIRECT_DEBIT, EXCHANGE, CARD_PAYMENT, WIRE, TRANSFER, WITHDRAWAL", "values": ["SEND_MONEY", "ADD_MONEY", "DIRECT_DEBIT", "EXCHANGE", "CARD_PAYMENT", "WIRE", "TRANSFER", "WITHDRAWAL"]},
    "tx_status":            {"col": "t.status",              "type": "enum",    "category": "transaction", "label": "Transaction Status",      "desc": "PENDING, SUCCESS, FAILED, BLOCKED, SUSPENDED, COMPLETED", "values": ["PENDING", "SUCCESS", "FAILED", "BLOCKED", "SUSPENDED", "COMPLETED"]},

    # ── Risk Score fields ──
    "risk_score":           {"col": "r.risk_score",          "type": "number",  "category": "risk",        "label": "Risk Score",              "desc": "Composite risk score 0–100"},
    "risk_level":           {"col": "r.risk_level",          "type": "enum",    "category": "risk",        "label": "Risk Level",              "desc": "LOW, MEDIUM, HIGH, CRITICAL", "values": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
    "velocity_score":       {"col": "r.velocity_score",      "type": "number",  "category": "risk",        "label": "Velocity Score",          "desc": "Velocity component 0–40"},
    "sanctions_score":      {"col": "r.sanctions_score",     "type": "number",  "category": "risk",        "label": "Sanctions Score",         "desc": "Sanctions match component 0–40"},
    "pattern_score":        {"col": "r.pattern_score",       "type": "number",  "category": "risk",        "label": "Pattern Score",           "desc": "Testing-the-waters pattern 0–25"},

    # ── Sender User Profile fields ──
    "sender_kyc_level":           {"col": "sp.kyc_level",              "type": "enum",   "category": "sender_profile", "label": "Sender KYC Level",            "desc": "NONE, BASIC, STANDARD, ENHANCED", "values": ["NONE", "BASIC", "STANDARD", "ENHANCED"]},
    "sender_kyc_status":          {"col": "sp.kyc_status",             "type": "enum",   "category": "sender_profile", "label": "Sender KYC Status",           "desc": "PENDING, VERIFIED, FAILED, EXPIRED, UNDER_REVIEW", "values": ["PENDING", "VERIFIED", "FAILED", "EXPIRED", "UNDER_REVIEW"]},
    "sender_nationality":         {"col": "sp.nationality",            "type": "string", "category": "sender_profile", "label": "Sender Nationality",          "desc": "ISO 3166-1 alpha-3 country code"},
    "sender_country":             {"col": "sp.country_of_residence",   "type": "string", "category": "sender_profile", "label": "Sender Country",              "desc": "Country of residence (alpha-3)"},
    "sender_city":                {"col": "sp.city",                   "type": "string", "category": "sender_profile", "label": "Sender City",                 "desc": "City of residence"},
    "sender_pep":                 {"col": "sp.pep_status",             "type": "boolean","category": "sender_profile", "label": "Sender PEP Status",           "desc": "Politically Exposed Person flag"},
    "sender_risk_rating":         {"col": "sp.risk_rating",            "type": "enum",   "category": "sender_profile", "label": "Sender Risk Rating",          "desc": "LOW, STANDARD, HIGH, VERY_HIGH, PROHIBITED", "values": ["LOW", "STANDARD", "HIGH", "VERY_HIGH", "PROHIBITED"]},
    "sender_occupation":          {"col": "sp.occupation",             "type": "string", "category": "sender_profile", "label": "Sender Occupation",           "desc": "Declared occupation"},
    "sender_source_of_funds":     {"col": "sp.source_of_funds",        "type": "enum",   "category": "sender_profile", "label": "Sender Source of Funds",      "desc": "SALARY, INVESTMENT, INHERITANCE, BUSINESS, OTHER", "values": ["SALARY", "INVESTMENT", "INHERITANCE", "BUSINESS", "OTHER"]},
    "sender_account_status":      {"col": "sp.account_status",         "type": "enum",   "category": "sender_profile", "label": "Sender Account Status",       "desc": "ACTIVE, SUSPENDED, CLOSED, FROZEN, PENDING", "values": ["ACTIVE", "SUSPENDED", "CLOSED", "FROZEN", "PENDING"]},
    "sender_total_tx_count":      {"col": "sp.total_transaction_count","type": "number", "category": "sender_profile", "label": "Sender Total Tx Count",       "desc": "Lifetime transaction count"},
    "sender_total_tx_volume":     {"col": "sp.total_transaction_volume","type": "number","category": "sender_profile", "label": "Sender Total Tx Volume",      "desc": "Lifetime transaction volume"},
    "sender_alert_count":         {"col": "sp.alert_count",            "type": "number", "category": "sender_profile", "label": "Sender Alert Count",          "desc": "Lifetime alert count"},
    "sender_previous_sar_count":  {"col": "sp.previous_sar_count",     "type": "number", "category": "sender_profile", "label": "Sender Previous SARs",        "desc": "Previous SAR filing count"},
    "sender_login_count":         {"col": "sp.login_count",            "type": "number", "category": "sender_profile", "label": "Sender Login Count",          "desc": "Total login count"},
    "sender_name":                {"col": "sp.full_name",              "type": "string", "category": "sender_profile", "label": "Sender Full Name",            "desc": "Full name from KYC"},
    "sender_email":               {"col": "sp.email",                  "type": "string", "category": "sender_profile", "label": "Sender Email",                "desc": "Email address"},

    # ── Receiver User Profile fields ──
    "receiver_kyc_level":         {"col": "rp.kyc_level",              "type": "enum",   "category": "receiver_profile", "label": "Receiver KYC Level",          "desc": "NONE, BASIC, STANDARD, ENHANCED", "values": ["NONE", "BASIC", "STANDARD", "ENHANCED"]},
    "receiver_kyc_status":        {"col": "rp.kyc_status",             "type": "enum",   "category": "receiver_profile", "label": "Receiver KYC Status",         "desc": "PENDING, VERIFIED, FAILED, EXPIRED, UNDER_REVIEW", "values": ["PENDING", "VERIFIED", "FAILED", "EXPIRED", "UNDER_REVIEW"]},
    "receiver_nationality":       {"col": "rp.nationality",            "type": "string", "category": "receiver_profile", "label": "Receiver Nationality",        "desc": "ISO 3166-1 alpha-3 country code"},
    "receiver_country":           {"col": "rp.country_of_residence",   "type": "string", "category": "receiver_profile", "label": "Receiver Country",            "desc": "Country of residence (alpha-3)"},
    "receiver_pep":               {"col": "rp.pep_status",             "type": "boolean","category": "receiver_profile", "label": "Receiver PEP Status",         "desc": "Politically Exposed Person flag"},
    "receiver_risk_rating":       {"col": "rp.risk_rating",            "type": "enum",   "category": "receiver_profile", "label": "Receiver Risk Rating",        "desc": "LOW, STANDARD, HIGH, VERY_HIGH, PROHIBITED", "values": ["LOW", "STANDARD", "HIGH", "VERY_HIGH", "PROHIBITED"]},
    "receiver_account_status":    {"col": "rp.account_status",         "type": "enum",   "category": "receiver_profile", "label": "Receiver Account Status",     "desc": "ACTIVE, SUSPENDED, CLOSED, FROZEN, PENDING", "values": ["ACTIVE", "SUSPENDED", "CLOSED", "FROZEN", "PENDING"]},
    "receiver_total_tx_count":    {"col": "rp.total_transaction_count","type": "number", "category": "receiver_profile", "label": "Receiver Total Tx Count",     "desc": "Lifetime transaction count"},
    "receiver_alert_count":       {"col": "rp.alert_count",            "type": "number", "category": "receiver_profile", "label": "Receiver Alert Count",        "desc": "Lifetime alert count"},
    "receiver_previous_sar_count":{"col": "rp.previous_sar_count",     "type": "number", "category": "receiver_profile", "label": "Receiver Previous SARs",      "desc": "Previous SAR filing count"},

    # ── Sender Card fields ──
    "sender_card_type":           {"col": "sc.card_type",              "type": "enum",   "category": "sender_card", "label": "Sender Card Type",             "desc": "VIRTUAL, PHYSICAL, PREPAID, DEBIT, CREDIT", "values": ["VIRTUAL", "PHYSICAL", "PREPAID", "DEBIT", "CREDIT"]},
    "sender_card_brand":          {"col": "sc.card_brand",             "type": "enum",   "category": "sender_card", "label": "Sender Card Brand",            "desc": "VISA, MASTERCARD, AMEX, OTHER", "values": ["VISA", "MASTERCARD", "AMEX", "OTHER"]},
    "sender_card_status":         {"col": "sc.card_status",            "type": "enum",   "category": "sender_card", "label": "Sender Card Status",           "desc": "ACTIVE, FROZEN, BLOCKED, EXPIRED, CANCELLED, PENDING", "values": ["ACTIVE", "FROZEN", "BLOCKED", "EXPIRED", "CANCELLED", "PENDING"]},
    "sender_card_country":        {"col": "sc.issuing_country",        "type": "string", "category": "sender_card", "label": "Sender Card Issuing Country",  "desc": "Card issuing country (alpha-3)"},
    "sender_card_daily_limit":    {"col": "sc.daily_limit",            "type": "number", "category": "sender_card", "label": "Sender Card Daily Limit",      "desc": "Card daily spending limit"},
    "sender_card_daily_spent":    {"col": "sc.daily_spent",            "type": "number", "category": "sender_card", "label": "Sender Card Daily Spent",      "desc": "Spent today on this card"},
    "sender_card_monthly_spent":  {"col": "sc.monthly_spent",          "type": "number", "category": "sender_card", "label": "Sender Card Monthly Spent",    "desc": "Spent this month"},
    "sender_card_total_spent":    {"col": "sc.total_spent",            "type": "number", "category": "sender_card", "label": "Sender Card Total Spent",      "desc": "Lifetime card spending"},
    "sender_card_declined_count": {"col": "sc.declined_count",         "type": "number", "category": "sender_card", "label": "Sender Card Declined Count",   "desc": "Times card was declined"},
    "sender_card_last_country":   {"col": "sc.last_used_country",      "type": "string", "category": "sender_card", "label": "Sender Card Last Country",     "desc": "Country where card was last used"},
    "sender_card_last_mcc":       {"col": "sc.last_used_mcc",          "type": "string", "category": "sender_card", "label": "Sender Card Last MCC",         "desc": "Merchant Category Code of last use"},
    "sender_card_contactless":    {"col": "sc.contactless_enabled",    "type": "boolean","category": "sender_card", "label": "Sender Contactless Enabled",   "desc": "Contactless payments enabled"},
    "sender_card_international":  {"col": "sc.international_enabled",  "type": "boolean","category": "sender_card", "label": "Sender International Enabled", "desc": "International payments enabled"},
}

# ── NLP synonym mapping → FIELD_CATALOG keys ──
FIELD_SYNONYMS: dict[str, str] = {}
for _key, _meta in FIELD_CATALOG.items():
    FIELD_SYNONYMS[_key] = _key
    FIELD_SYNONYMS[_meta["label"].lower()] = _key
    FIELD_SYNONYMS[_key.replace("_", " ")] = _key

_EXTRA_SYNONYMS = {
    "amount": "amount", "value": "amount", "sum": "amount", "transaction amount": "amount",
    "sender": "sender_id", "from": "sender_id",
    "receiver": "receiver_id", "to": "receiver_id", "beneficiary": "receiver_id",
    "currency": "currency", "type": "transaction_type",
    "risk score": "risk_score", "risk": "risk_score", "score": "risk_score",
    "velocity": "velocity_score", "pattern": "pattern_score",
    "kyc level": "sender_kyc_level", "kyc": "sender_kyc_level", "kyc status": "sender_kyc_status",
    "nationality": "sender_nationality", "country": "sender_country", "city": "sender_city",
    "pep": "sender_pep", "politically exposed": "sender_pep",
    "risk rating": "sender_risk_rating", "user risk": "sender_risk_rating",
    "occupation": "sender_occupation", "job": "sender_occupation",
    "source of funds": "sender_source_of_funds", "fund source": "sender_source_of_funds",
    "account status": "sender_account_status",
    "total transactions": "sender_total_tx_count", "tx count": "sender_total_tx_count",
    "transaction volume": "sender_total_tx_volume", "tx volume": "sender_total_tx_volume",
    "alert count": "sender_alert_count", "alerts": "sender_alert_count",
    "sar count": "sender_previous_sar_count", "previous sar": "sender_previous_sar_count",
    "card type": "sender_card_type", "card": "sender_card_type",
    "card brand": "sender_card_brand", "card status": "sender_card_status",
    "card country": "sender_card_country", "issuing country": "sender_card_country",
    "daily limit": "sender_card_daily_limit", "daily spent": "sender_card_daily_spent",
    "monthly spent": "sender_card_monthly_spent",
    "declined": "sender_card_declined_count", "decline count": "sender_card_declined_count",
    "mcc": "sender_card_last_mcc", "merchant category": "sender_card_last_mcc",
    "contactless": "sender_card_contactless", "international": "sender_card_international",
    "sender name": "sender_name", "sender email": "sender_email",
    "receiver kyc": "receiver_kyc_level", "receiver country": "receiver_country",
    "receiver nationality": "receiver_nationality",
    "receiver pep": "receiver_pep", "receiver risk": "receiver_risk_rating",
    "receiver account status": "receiver_account_status",
    "receiver alerts": "receiver_alert_count", "receiver sar": "receiver_previous_sar_count",
}
FIELD_SYNONYMS.update(_EXTRA_SYNONYMS)

OPERATOR_MAP = {
    "greater than": "gt", "more than": "gt", "above": "gt", "over": "gt",
    "exceeds": "gt", "higher than": "gt", ">": "gt",
    "less than": "lt", "below": "lt", "under": "lt", "lower than": "lt", "<": "lt",
    "equals": "eq", "equal to": "eq", "is": "eq", "=": "eq", "==": "eq",
    "not": "neq", "not equal": "neq", "!=": "neq", "is not": "neq",
    "contains": "contains", "includes": "contains", "has": "contains", "like": "contains",
    "in": "in", "one of": "in",
    "true": "is_true", "enabled": "is_true", "yes": "is_true",
    "false": "is_false", "disabled": "is_false", "no": "is_false",
}

ACTION_MAP = {
    "block": "BLOCK", "stop": "BLOCK", "reject": "BLOCK", "deny": "BLOCK",
    "suspend": "SUSPEND", "hold": "SUSPEND", "freeze": "SUSPEND", "pause": "SUSPEND",
    "flag": "FLAG", "review": "FLAG", "alert": "FLAG", "warn": "FLAG", "monitor": "FLAG",
    "allow": "ALLOW", "approve": "ALLOW", "pass": "ALLOW",
}

SEVERITY_MAP = {
    "critical": "CRITICAL", "very high": "CRITICAL", "urgent": "CRITICAL",
    "high": "HIGH", "important": "HIGH",
    "medium": "MEDIUM", "moderate": "MEDIUM", "normal": "MEDIUM",
    "low": "LOW", "minor": "LOW", "informational": "LOW",
}

CATEGORIES = {
    "transaction":       {"label": "Transaction",       "icon": "ArrowLeftRight", "color": "#818cf8"},
    "risk":              {"label": "Risk Score",         "icon": "ShieldAlert",    "color": "#f87171"},
    "sender_profile":    {"label": "Sender Profile",     "icon": "User",           "color": "#34d399"},
    "receiver_profile":  {"label": "Receiver Profile",   "icon": "UserCheck",      "color": "#60a5fa"},
    "sender_card":       {"label": "Sender Card",        "icon": "CreditCard",     "color": "#fbbf24"},
}


# ── Helpers ──────────────────────────────────────────────────────────

def _parse_number(txt: str) -> str | None:
    txt = txt.replace(",", "").replace("$", "").replace("€", "").replace("£", "").strip()
    m = re.search(r"([\d.]+)\s*([kmb])?", txt, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    elif suffix == "b":
        num *= 1_000_000_000
    return str(int(num)) if num == int(num) else str(num)


def _find_best_match(txt: str, mapping: dict[str, str]) -> tuple[str | None, str]:
    txt_lower = txt.lower()
    for key in sorted(mapping.keys(), key=len, reverse=True):
        if key in txt_lower:
            return mapping[key], key
    return None, ""


def _explain_rule(rule: ParsedRule) -> str:
    parts = []
    for cond in rule.conditions:
        meta = FIELD_CATALOG.get(cond["field"], {})
        label = meta.get("label", cond["field"])
        cat = CATEGORIES.get(meta.get("category", ""), {}).get("label", "")
        op_text = {
            "gt": "greater than", "lt": "less than", "eq": "equal to",
            "neq": "not equal to", "contains": "containing", "in": "one of",
        }.get(cond["operator"], cond["operator"])
        prefix = f"[{cat}] " if cat else ""
        if cond["operator"] == "is_true":
            parts.append(f"{prefix}**{label}** is **true**")
        elif cond["operator"] == "is_false":
            parts.append(f"{prefix}**{label}** is **false**")
        else:
            parts.append(f"{prefix}**{label}** is {op_text} **{cond['value']}**")
    logic_text = f" {rule.logic} ".lower()
    return f"When {logic_text.join(parts)}, **{rule.action}** the transaction (severity: {rule.severity}, risk: +{rule.risk_score_adjustment})"


# ── NLP Parser ───────────────────────────────────────────────────────

def parse_english_to_rule(message: str) -> ParsedRule:
    msg = message.strip()
    msg_lower = msg.lower()

    # Detect action
    action = "FLAG"
    for kw in sorted(ACTION_MAP.keys(), key=len, reverse=True):
        if kw in msg_lower:
            action = ACTION_MAP[kw]
            break

    # Detect severity
    severity = "HIGH" if action in ("BLOCK", "SUSPEND") else "MEDIUM"
    for kw in sorted(SEVERITY_MAP.keys(), key=len, reverse=True):
        if kw in msg_lower:
            severity = SEVERITY_MAP[kw]
            break

    risk_adj = {"BLOCK": 50, "SUSPEND": 40, "FLAG": 25, "ALLOW": 0}.get(action, 25)

    # Parse conditions
    conditions: list[dict] = []
    logic = "OR" if " or " in msg_lower else "AND"
    parts = re.split(r"\band\b|\bor\b", msg, flags=re.IGNORECASE)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # ── Priority check: detect boolean PEP phrases before generic "sender" ──
        part_lower = part.lower()
        pep_field = None
        if re.search(r"\breceiver\b.*\bpep\b|\bpep\b.*\breceiver\b", part_lower):
            pep_field = "receiver_pep"
        elif re.search(r"\bsender\b.*\bpep\b|\bpep\b.*\bsender\b|\bis pep\b|\bpep status\b|\bpep is\b", part_lower):
            pep_field = "sender_pep"
        if pep_field:
            meta = FIELD_CATALOG[pep_field]
            if any(w in part_lower for w in ["false", "not", "no", "disabled"]):
                conditions.append({"field": pep_field, "operator": "is_false", "value": "false", "category": meta["category"]})
            else:
                conditions.append({"field": pep_field, "operator": "is_true", "value": "true", "category": meta["category"]})
            continue

        field_key, field_match = _find_best_match(part, FIELD_SYNONYMS)
        if not field_key:
            if re.search(r"amount|value|\$|€|£|\d{3,}", part, re.IGNORECASE):
                field_key = "amount"
                field_match = ""
            else:
                continue

        meta = FIELD_CATALOG.get(field_key, {})
        after_field = part.lower().split(field_match)[-1].strip() if field_match else part.lower()

        # Boolean fields
        if meta.get("type") == "boolean":
            if any(w in part_lower for w in ["true", "yes", "enabled", "is pep", "are pep"]):
                conditions.append({"field": field_key, "operator": "is_true", "value": "true", "category": meta.get("category", "")})
            else:
                conditions.append({"field": field_key, "operator": "is_false", "value": "false", "category": meta.get("category", "")})
            continue

        # For enum fields, try direct enum value match BEFORE operator matching
        # This prevents "NONE" being eaten by the "no"→is_false operator
        # Sort by length descending so VERY_HIGH matches before HIGH
        if meta.get("type") == "enum":
            enum_matched = False
            for ev in sorted(meta.get("values", []), key=len, reverse=True):
                if ev.lower() in part_lower:
                    conditions.append({"field": field_key, "operator": "eq", "value": ev, "category": meta.get("category", "")})
                    enum_matched = True
                    break
            if enum_matched:
                continue

        # Try operator match
        op_val, op_key = _find_best_match(after_field, OPERATOR_MAP)
        if op_val:
            after_op = after_field.split(op_key)[-1].strip()
            value = None
            if op_val in ("gt", "lt"):
                value = _parse_number(after_op)
            elif op_val == "in":
                value = ",".join(v.strip().strip("'\"") for v in after_op.split(",") if v.strip())
            elif op_val in ("is_true", "is_false"):
                value = "true" if op_val == "is_true" else "false"
            else:
                value = after_op.strip("'\" .,;")
                # Uppercase country codes and short string values
                if value and meta.get("type") == "string" and len(value) <= 5:
                    value = value.upper()

            # Normalize enum values to canonical uppercase form
            if value and meta.get("type") == "enum":
                for ev in meta.get("values", []):
                    if ev.lower() == value.lower():
                        value = ev
                        break

            if value:
                conditions.append({"field": field_key, "operator": op_val, "value": value, "category": meta.get("category", "")})
                continue

        # Infer operator
        num = _parse_number(part)
        if num and meta.get("type") == "number":
            op = "gt" if any(w in part_lower for w in ["over", "above", "more", "greater", "exceed", ">"]) else \
                 "lt" if any(w in part_lower for w in ["under", "below", "less", "<"]) else "gt"
            conditions.append({"field": field_key, "operator": op, "value": num, "category": meta.get("category", "")})
        elif meta.get("type") == "enum":
            for ev in sorted(meta.get("values", []), key=len, reverse=True):
                if ev.lower() in part_lower:
                    conditions.append({"field": field_key, "operator": "eq", "value": ev, "category": meta.get("category", "")})
                    break
        else:
            remaining = after_field.strip()
            for skip in list(ACTION_MAP.keys()) + list(SEVERITY_MAP.keys()):
                remaining = re.sub(rf"\b{re.escape(skip)}\b", "", remaining, flags=re.IGNORECASE).strip()
            remaining = remaining.strip("'\" .,;")
            if remaining:
                # Normalize enum values to canonical form
                if meta.get("type") == "enum":
                    for ev in meta.get("values", []):
                        if ev.lower() == remaining.lower():
                            remaining = ev
                            break
                op = "eq" if meta.get("type") in ("enum", "string") and len(remaining.split()) <= 2 else "contains"
                conditions.append({"field": field_key, "operator": op, "value": remaining, "category": meta.get("category", "")})

    # Fallback
    if not conditions:
        m = re.search(r"(amount|transactions?|value).*?(over|above|greater|more|below|under|less)\s+\$?([\d,.]+[kmb]?)", msg, re.IGNORECASE)
        if m:
            op = "gt" if m.group(2).lower() in ("over", "above", "greater", "more") else "lt"
            conditions.append({"field": "amount", "operator": op, "value": _parse_number(m.group(3)) or m.group(3), "category": "transaction"})

    if not conditions:
        raise ValueError(
            "Could not parse conditions. Try: 'Block transactions over $10,000' "
            "or 'Flag sender with KYC level NONE and amount above 5k'"
        )

    rule_name = re.sub(r"[^a-z0-9]+", "_", msg_lower[:60]).strip("_")

    return ParsedRule(
        rule_name=rule_name, description=msg, conditions=conditions,
        logic=logic, action=action, risk_score_adjustment=risk_adj, severity=severity,
    )


# ── SQL builder for backtesting (cross-table JOINs) ─────────────────

ALLOWED_OPERATORS = {"gt", "lt", "eq", "neq", "contains", "in", "is_true", "is_false"}


def _needs_join(conditions: list[dict]) -> dict[str, bool]:
    joins = {"risk": False, "sender_profile": False, "receiver_profile": False, "sender_card": False}
    for cond in conditions:
        meta = FIELD_CATALOG.get(cond.get("field", ""), {})
        cat = meta.get("category", "")
        if cat == "risk":
            joins["risk"] = True
        elif cat == "sender_profile":
            joins["sender_profile"] = True
        elif cat == "receiver_profile":
            joins["receiver_profile"] = True
        elif cat == "sender_card":
            joins["sender_card"] = True
    return joins


def _build_from_clause(joins: dict[str, bool]) -> str:
    s = get_settings().postgres_schema
    sql = f"FROM {s}.transactions t"
    if joins["risk"]:
        sql += f"\nLEFT JOIN {s}.risk_scores r ON t.id::text = r.transaction_id"
    if joins["sender_profile"]:
        sql += f"\nLEFT JOIN {s}.user_profiles sp ON t.sender_id = sp.user_id"
    if joins["receiver_profile"]:
        sql += f"\nLEFT JOIN {s}.user_profiles rp ON t.receiver_id = rp.user_id"
    if joins["sender_card"]:
        sql += f"\nLEFT JOIN {s}.user_cards sc ON t.sender_id = sc.user_id"
    return sql


def _build_select_clause(joins: dict[str, bool]) -> str:
    cols = [
        "t.id", "t.external_id", "t.sender_id", "t.receiver_id",
        "t.amount", "t.currency", "t.transaction_type", "t.status AS tx_status",
        "t.created_at",
    ]
    if joins["risk"]:
        cols += ["r.risk_score", "r.risk_level", "r.velocity_score", "r.sanctions_score", "r.pattern_score"]
    if joins["sender_profile"]:
        cols += [
            "sp.full_name AS sender_name", "sp.kyc_level AS sender_kyc", "sp.kyc_status AS sender_kyc_status",
            "sp.nationality AS sender_nationality", "sp.country_of_residence AS sender_country",
            "sp.pep_status AS sender_pep", "sp.risk_rating AS sender_risk_rating",
            "sp.account_status AS sender_acct_status", "sp.total_transaction_count AS sender_tx_count",
            "sp.alert_count AS sender_alerts", "sp.previous_sar_count AS sender_sars",
        ]
    if joins["receiver_profile"]:
        cols += [
            "rp.full_name AS receiver_name", "rp.kyc_level AS receiver_kyc",
            "rp.country_of_residence AS receiver_country", "rp.pep_status AS receiver_pep",
            "rp.risk_rating AS receiver_risk_rating",
        ]
    if joins["sender_card"]:
        cols += [
            "sc.card_type AS sender_card_type", "sc.card_brand AS sender_card_brand",
            "sc.card_status AS sender_card_status", "sc.issuing_country AS sender_card_country",
            "sc.daily_spent AS sender_daily_spent", "sc.declined_count AS sender_card_declines",
        ]
    return "SELECT " + ",\n       ".join(cols)


def _build_condition_sql(conditions: list[dict], logic: str = "AND") -> tuple[str, dict]:
    clauses = []
    params: dict = {}

    for i, cond in enumerate(conditions):
        field = cond.get("field", "")
        operator = cond.get("operator", "")
        value = cond.get("value", "")

        meta = FIELD_CATALOG.get(field)
        if not meta or operator not in ALLOWED_OPERATORS:
            continue

        col = meta["col"]
        pname = f"p{i}"

        if operator == "gt":
            clauses.append(f"{col} > :{pname}")
            params[pname] = float(value)
        elif operator == "lt":
            clauses.append(f"{col} < :{pname}")
            params[pname] = float(value)
        elif operator == "eq":
            clauses.append(f"{col} = :{pname}")
            params[pname] = value
        elif operator == "neq":
            clauses.append(f"{col} != :{pname}")
            params[pname] = value
        elif operator == "contains":
            clauses.append(f"{col} ILIKE :{pname}")
            params[pname] = f"%{value}%"
        elif operator == "in":
            vals = [v.strip() for v in value.split(",")]
            in_names = [f"{pname}_{j}" for j in range(len(vals))]
            placeholders = ", ".join(f":{n}" for n in in_names)
            clauses.append(f"{col} IN ({placeholders})")
            for j, v in enumerate(vals):
                params[f"{pname}_{j}"] = v
        elif operator == "is_true":
            clauses.append(f"{col} = TRUE")
        elif operator == "is_false":
            clauses.append(f"{col} = FALSE")

    if not clauses:
        return "FALSE", {}

    return f" {logic} ".join(clauses), params


# ── Endpoints ────────────────────────────────────────────────────────

@router.get("/fields")
async def get_fields():
    grouped: dict[str, list] = {}
    for key, meta in FIELD_CATALOG.items():
        cat = meta["category"]
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({
            "field": key,
            "label": meta["label"],
            "type": meta["type"],
            "description": meta["desc"],
            "values": meta.get("values"),
        })

    return {
        "categories": {
            k: {"label": v["label"], "icon": v["icon"], "color": v["color"], "fields": grouped.get(k, [])}
            for k, v in CATEGORIES.items()
        },
        "total_fields": len(FIELD_CATALOG),
    }


@router.post("/parse")
async def parse_rule(body: ParseRequest):
    try:
        parsed = parse_english_to_rule(body.message)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "original_message": body.message,
        "parsed_rule": parsed.model_dump(),
        "explanation": _explain_rule(parsed),
    }


@router.post("/backtest")
async def backtest_rule(body: BacktestRequest, db: AsyncSession = Depends(get_db)):
    where_sql, params = _build_condition_sql(body.conditions, body.logic)
    if where_sql == "FALSE":
        raise HTTPException(status_code=422, detail="No valid conditions to test")

    joins = _needs_join(body.conditions)
    from_clause = _build_from_clause(joins)
    select_clause = _build_select_clause(joins)

    params["lim"] = body.limit

    try:
        result = await db.execute(
            text(f"{select_clause}\n{from_clause}\nWHERE {where_sql}\nORDER BY t.created_at DESC\nLIMIT :lim"),
            params,
        )
        rows = result.fetchall()

        count_params = {k: v for k, v in params.items() if k != "lim"}
        count_result = await db.execute(
            text(f"SELECT COUNT(*) AS total\n{from_clause}\nWHERE {where_sql}"),
            count_params,
        )
        total_matched = count_result.scalar() or 0

        total_result = await db.execute(text(f"SELECT COUNT(*) FROM {get_settings().postgres_schema}.transactions"))
        total_transactions = total_result.scalar() or 0

        matches = [dict(r._mapping) for r in rows]
    except Exception as e:
        logger.error(f"Backtest DB query failed: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable. Start PostgreSQL with 'docker compose up -d postgres' to enable backtesting.")

    risk_distribution: dict[str, int] = {}
    for m in matches:
        level = m.get("risk_level") or m.get("sender_risk_rating") or "UNKNOWN"
        risk_distribution[level] = risk_distribution.get(level, 0) + 1

    categories_used = list({cond.get("category", "transaction") for cond in body.conditions})

    return {
        "total_transactions": total_transactions,
        "total_matched": total_matched,
        "match_rate": round(total_matched / max(total_transactions, 1) * 100, 2),
        "sample_matches": matches[:50],
        "sample_size": len(matches),
        "risk_distribution": risk_distribution,
        "categories_used": categories_used,
        "joins_used": {k: v for k, v in joins.items() if v},
    }


@router.post("/deploy")
async def deploy_rule(body: DeployRequest, db: AsyncSession = Depends(get_db)):
    rule_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Persist to Postgres so the rule survives restarts and is visible to all replicas
    await register_rule(
        rule_id=rule_id,
        rule_name=body.rule_name,
        description=body.description,
        conditions=body.conditions,
        action=body.action,
        risk_score_adjustment=body.risk_score_adjustment,
        severity=body.severity,
        created_by="rule-chat",
        db=db,
    )

    try:
        producer = await wait_for(get_kafka_producer(), timeout=3.0)
        for cond in body.conditions:
            kafka_msg = {
                "rule_id": rule_id,
                "rule_name": body.rule_name,
                "description": body.description,
                "condition": {k: v for k, v in cond.items() if k != "category"},
                "action": body.action,
                "risk_score_adjustment": body.risk_score_adjustment,
                "severity": body.severity,
                "active": True,
                "created_by": "rule-chat",
                "created_at": now,
                "version": 1,
            }
            await wait_for(
                producer.send_and_wait(
                    "detection-rules",
                    key=rule_id.encode(),
                    value=json.dumps(kafka_msg).encode(),
                ),
                timeout=3.0,
            )
        logger.info(f"Rule {rule_id} published to Kafka detection-rules topic")
    except AsyncTimeoutError:
        logger.warning(f"Kafka publish timed out for rule {rule_id} (rule still saved locally)")
    except Exception as e:
        logger.error(f"Kafka publish failed: {e}")

    return {
        "status": "deployed",
        "rule_id": rule_id,
        "rule_name": body.rule_name,
        "conditions": body.conditions,
        "action": body.action,
        "severity": body.severity,
        "message": f"Rule '{body.rule_name}' deployed → Kafka → Flink will apply within seconds.",
    }


@router.post("/edit")
async def edit_rule_ai(body: EditRequest, db: AsyncSession = Depends(get_db)):
    """Edit an existing rule using natural language instructions."""
    rule = await fetch_rule(body.rule_id, db=db)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    msg = body.message.lower()

    # Detect what to change from the English instruction
    updates: dict = {}

    # Action changes
    for kw, act in ACTION_MAP.items():
        if kw in msg and act != rule.get("action"):
            updates["action"] = act
            break

    # Severity changes
    for kw, sev in SEVERITY_MAP.items():
        if kw in msg and sev != rule.get("severity"):
            updates["severity"] = sev
            break

    # Risk score adjustment
    risk_match = re.search(r"risk.*?(\d+)", msg) or re.search(r"adjustment.*?(\d+)", msg) or re.search(r"\+\s*(\d+)", msg)
    if risk_match:
        updates["risk_score_adjustment"] = int(risk_match.group(1))

    # Threshold/value changes  (e.g. "change threshold to 25000", "set amount to 10000")
    threshold_match = re.search(r"(?:threshold|value|amount|limit).*?(?:to|=)\s*(\$?[\d,.]+[kmb]?)", msg, re.IGNORECASE)
    if threshold_match:
        new_val = _parse_number(threshold_match.group(1))
        if new_val:
            cond = rule.get("condition_json", {})
            cond["value"] = new_val
            updates["condition_json"] = cond

    # Field changes ("change field to velocity_score")
    field_match = re.search(r"field.*?to\s+(\w+)", msg)
    if field_match and field_match.group(1) in FIELD_CATALOG:
        cond = updates.get("condition_json", dict(rule.get("condition_json", {})))
        cond["field"] = field_match.group(1)
        updates["condition_json"] = cond

    # Operator changes
    op_match = re.search(r"operator.*?to\s+(\w[\w ]*)", msg)
    if op_match:
        op_val, _ = _find_best_match(op_match.group(1), OPERATOR_MAP)
        if op_val:
            cond = updates.get("condition_json", dict(rule.get("condition_json", {})))
            cond["operator"] = op_val
            updates["condition_json"] = cond

    # Description changes
    desc_match = re.search(r"description.*?(?:to|:)\s+[\"\']?(.+?)[\"\']?$", msg)
    if desc_match:
        updates["description"] = desc_match.group(1).strip()

    # Active/inactive toggle
    if any(w in msg for w in ["deactivate", "disable", "turn off", "inactive"]):
        updates["active"] = False
    elif any(w in msg for w in ["activate", "enable", "turn on"]):
        updates["active"] = True

    if not updates:
        raise HTTPException(status_code=422, detail="Could not understand what to change. Try: 'change action to BLOCK', 'set threshold to 25000', 'change severity to CRITICAL'")

    # Persist to Postgres
    rule = await apply_rule_updates(body.rule_id, updates, db=db)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    # Kafka publish
    try:
        producer = await wait_for(get_kafka_producer(), timeout=3.0)
        await wait_for(
            producer.send_and_wait(
                "detection-rules",
                key=body.rule_id.encode(),
                value=json.dumps({"rule_id": body.rule_id, **{k: v for k, v in rule.items() if k not in ("created_at",)}, "updated_at": rule["updated_at"]}).encode(),
            ),
            timeout=3.0,
        )
    except (AsyncTimeoutError, Exception) as e:
        logger.warning(f"Kafka publish failed on edit: {e}")

    changes_desc = ", ".join(f"{k}={v}" for k, v in updates.items())
    return {
        "status": "updated",
        "rule_id": body.rule_id,
        "rule_name": rule["rule_name"],
        "changes": updates,
        "changes_description": changes_desc,
        "rule": rule,
        "message": f"Rule '{rule['rule_name']}' updated: {changes_desc}",
    }


@router.post("/edit-direct")
async def edit_rule_direct(body: DirectEditRequest, db: AsyncSession = Depends(get_db)):
    """Edit an existing rule with explicit field values (from UI edit form)."""
    rule = await fetch_rule(body.rule_id, db=db)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    updates: dict = {}
    if body.description is not None:
        updates["description"] = body.description
    if body.action is not None:
        updates["action"] = body.action
    if body.risk_score_adjustment is not None:
        updates["risk_score_adjustment"] = body.risk_score_adjustment
    if body.severity is not None:
        updates["severity"] = body.severity
    if body.conditions is not None:
        if len(body.conditions) == 1:
            cond = body.conditions[0]
            updates["condition_json"] = {"field": cond.get("field", ""), "operator": cond.get("operator", ""), "value": str(cond.get("value", ""))}
        else:
            updates["condition_json"] = {
                "field": " + ".join(c.get("field", "") for c in body.conditions),
                "operator": "multi",
                "value": " AND ".join(f'{c["field"]} {c["operator"]} {c["value"]}' for c in body.conditions),
            }

    if not updates:
        raise HTTPException(status_code=422, detail="No changes provided")

    rule = await apply_rule_updates(body.rule_id, updates, db=db)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    try:
        producer = await wait_for(get_kafka_producer(), timeout=3.0)
        await wait_for(
            producer.send_and_wait(
                "detection-rules",
                key=body.rule_id.encode(),
                value=json.dumps({"rule_id": body.rule_id, **rule}).encode(),
            ),
            timeout=3.0,
        )
    except (AsyncTimeoutError, Exception) as e:
        logger.warning(f"Kafka publish failed on edit: {e}")

    return {"status": "updated", "rule": rule}


@router.get("/suggestions")
async def get_suggestions():
    return {
        "suggestions": [
            # ── Transaction ──
            {"text": "Block transactions over $50,000", "category": "transaction"},
            {"text": "Suspend if amount above 25k and currency is USD", "category": "transaction"},

            # ── Risk ──
            {"text": "Block if risk score above 85 and velocity score over 30", "category": "risk"},
            {"text": "Flag transactions with sanctions score above 20", "category": "risk"},

            # ── Sender Profile ──
            {"text": "Block if sender is PEP and amount over 10k", "category": "sender_profile"},
            {"text": "Suspend if sender risk rating is VERY_HIGH and KYC level is NONE", "category": "sender_profile"},
            {"text": "Flag if sender previous SAR count greater than 0 and amount above 5000", "category": "sender_profile"},
            {"text": "Block if sender country is IRN and sender account status is FROZEN", "category": "sender_profile"},
            {"text": "Flag if sender total tx count greater than 100 and alert count above 3", "category": "sender_profile"},

            # ── Receiver Profile ──
            {"text": "Block if receiver PEP is true and amount over 25k", "category": "receiver_profile"},
            {"text": "Suspend if receiver KYC status is FAILED and receiver risk rating is VERY_HIGH", "category": "receiver_profile"},
            {"text": "Flag if receiver previous SAR count greater than 0 and amount above 10000", "category": "receiver_profile"},

            # ── Sender Card ──
            {"text": "Suspend if sender card type is PREPAID and amount over 3000", "category": "sender_card"},
            {"text": "Flag if sender card declined count above 5 and sender card brand is VISA", "category": "sender_card"},
            {"text": "Block if sender card status is FROZEN and sender card type is VIRTUAL", "category": "sender_card"},

            # ── Multi-Entity (cross-category) ──
            {"text": "Block if sender is PEP and sender card type is VIRTUAL and amount over 10000", "category": "sender_profile"},
            {"text": "Suspend if risk score above 70 and sender KYC level is BASIC and receiver country is SYR", "category": "risk"},
            {"text": "Flag if sender card monthly spent over 50000 and sender risk rating is HIGH", "category": "sender_card"},
            {"text": "Block if amount over 100k and sender source of funds is OTHER and receiver PEP is true", "category": "transaction"},
        ]
    }
