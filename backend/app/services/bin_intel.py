"""
BIN / IIN intelligence — Gap 7.

Offline + optional live BIN (Bank Identification Number) lookup.

The *first* 6-8 digits of a card number identify the issuing institution,
the card scheme, type (credit/debit/prepaid), and issuing country. third-party vendor's
"card.bin.*" feature set is replicated here with:

  1. An offline fallback map for the most common schemes (scheme detection
     from length+prefix per ISO/IEC 7812, which needs no network access).
  2. An optional live lookup against the free binlist.net JSON API,
     rate-limited to 5 req/s per their terms and cached in-process for 24h.

Live lookups are gated by the ``AFDS_ENABLE_BIN_LIVE`` env var (default "1").
Downstream callers should treat the response as best-effort: a network
failure simply returns the offline fields.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 24 * 3600
_CACHE_MAX = 5000
_REQ_SEMAPHORE = asyncio.Semaphore(5)
_LIVE_TIMEOUT = 2.5

# ── ISO/IEC 7812 scheme prefixes (offline) ──────────────────────────────
# Ordered: longest prefix match wins.
_SCHEME_PREFIXES: list[tuple[str, str]] = [
    ("4", "VISA"),
    ("34", "AMEX"), ("37", "AMEX"),
    ("300", "DINERS"), ("301", "DINERS"), ("302", "DINERS"),
    ("303", "DINERS"), ("304", "DINERS"), ("305", "DINERS"),
    ("36", "DINERS"), ("38", "DINERS"), ("39", "DINERS"),
    ("51", "MASTERCARD"), ("52", "MASTERCARD"), ("53", "MASTERCARD"),
    ("54", "MASTERCARD"), ("55", "MASTERCARD"),
    ("2221", "MASTERCARD"), ("2222", "MASTERCARD"), ("2223", "MASTERCARD"),
    ("2224", "MASTERCARD"), ("2225", "MASTERCARD"), ("2226", "MASTERCARD"),
    ("2227", "MASTERCARD"), ("2228", "MASTERCARD"), ("2229", "MASTERCARD"),
    ("223", "MASTERCARD"), ("224", "MASTERCARD"), ("225", "MASTERCARD"),
    ("226", "MASTERCARD"), ("227", "MASTERCARD"), ("228", "MASTERCARD"),
    ("229", "MASTERCARD"),
    ("23", "MASTERCARD"), ("24", "MASTERCARD"), ("25", "MASTERCARD"),
    ("26", "MASTERCARD"), ("270", "MASTERCARD"), ("271", "MASTERCARD"),
    ("2720", "MASTERCARD"),
    ("6011", "DISCOVER"), ("65", "DISCOVER"),
    ("644", "DISCOVER"), ("645", "DISCOVER"), ("646", "DISCOVER"),
    ("647", "DISCOVER"), ("648", "DISCOVER"), ("649", "DISCOVER"),
    ("35", "JCB"),
    ("62", "UNIONPAY"),
    ("50", "MAESTRO"), ("56", "MAESTRO"), ("57", "MAESTRO"),
    ("58", "MAESTRO"), ("63", "MAESTRO"), ("67", "MAESTROUK"),
]


def _classify_offline(bin6: str) -> dict[str, Any]:
    """Return scheme + basic validity from the 6-digit BIN alone."""
    out: dict[str, Any] = {
        "bin": bin6,
        "scheme": None,
        "type": None,
        "brand": None,
        "bank": None,
        "country": None,
        "country_code": None,
        "prepaid": None,
        "source": "offline",
    }
    best = ""
    for prefix, scheme in _SCHEME_PREFIXES:
        if bin6.startswith(prefix) and len(prefix) > len(best):
            best = prefix
            out["scheme"] = scheme
    return out


async def _fetch_live(bin6: str) -> dict[str, Any] | None:
    """Best-effort binlist.net lookup. Returns None on any error."""
    url = f"https://lookup.binlist.net/{bin6}"
    try:
        async with _REQ_SEMAPHORE:
            async with httpx.AsyncClient(timeout=_LIVE_TIMEOUT) as client:
                r = await client.get(url, headers={"Accept-Version": "3"})
        if r.status_code != 200:
            return None
        data = r.json() or {}
        country = data.get("country") or {}
        bank = data.get("bank") or {}
        return {
            "scheme": (data.get("scheme") or "").upper() or None,
            "type": (data.get("type") or "").upper() or None,
            "brand": (data.get("brand") or "").upper() or None,
            "bank": bank.get("name"),
            "country": country.get("name"),
            "country_code": country.get("alpha2"),
            "prepaid": data.get("prepaid"),
            "source": "binlist.net",
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("binlist.net lookup failed for %s: %s", bin6, exc)
        return None


def _prune_cache() -> None:
    if len(_CACHE) < _CACHE_MAX:
        return
    # drop oldest 10%
    victims = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 10]
    for k, _ in victims:
        _CACHE.pop(k, None)


async def lookup_bin(bin_input: str) -> dict[str, Any]:
    """Look up a BIN (6–8 digits). Combines offline classification with
    optional live enrichment from binlist.net.  Cached for 24h."""
    digits = "".join(ch for ch in (bin_input or "") if ch.isdigit())
    if len(digits) < 6:
        return {
            "bin": digits,
            "valid": False,
            "reason": "bin_too_short",
            "source": "offline",
        }
    bin6 = digits[:6]

    # cache
    cached = _CACHE.get(bin6)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return dict(cached[1])

    offline = _classify_offline(bin6)
    offline["valid"] = offline.get("scheme") is not None

    if os.getenv("AFDS_ENABLE_BIN_LIVE", "1") == "1":
        live = await _fetch_live(bin6)
        if live:
            for k, v in live.items():
                if v is not None:
                    offline[k] = v
            offline["source"] = "binlist.net"

    _prune_cache()
    _CACHE[bin6] = (time.time(), dict(offline))
    return offline


def derive_risk_signals(bin_intel: dict[str, Any], expected_country: str | None = None) -> dict[str, Any]:
    """Turn a BIN lookup result into AFDS risk signals.

    ``expected_country`` (ISO alpha-2) — if provided and the card was issued
    in a different country, contributes a ``card_country_mismatch`` factor.
    """
    signals: dict[str, Any] = {
        "risk_factors": [],
        "risk_score": 0,
        "bin": bin_intel.get("bin"),
        "scheme": bin_intel.get("scheme"),
        "type": bin_intel.get("type"),
        "brand": bin_intel.get("brand"),
        "issuer_country": bin_intel.get("country_code"),
        "issuer_bank": bin_intel.get("bank"),
        "prepaid": bin_intel.get("prepaid"),
    }
    if not bin_intel.get("valid"):
        signals["risk_factors"].append("unknown_bin_scheme")
        signals["risk_score"] += 10
        return signals
    if bin_intel.get("prepaid") is True:
        signals["risk_factors"].append("prepaid_card")
        signals["risk_score"] += 20
    btype = (bin_intel.get("type") or "").upper()
    if btype in {"DEBIT"}:
        # no inherent risk, just informational
        pass
    if expected_country and bin_intel.get("country_code"):
        if expected_country.upper() != (bin_intel["country_code"] or "").upper():
            signals["risk_factors"].append("card_country_mismatch")
            signals["risk_score"] += 25
    return signals
