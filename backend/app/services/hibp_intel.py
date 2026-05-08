"""
HIBP breach intelligence (Gap 1) — free-tier integration.

HIBP's full breach-by-email API requires a paid key, but two endpoints
are free of charge and good third-party vendor proxies:

  * ``api.pwnedpasswords.com/range/{prefix}`` — Pwned Passwords
    k-anonymity lookup. The client hashes the password with SHA-1,
    sends only the first 5 hex chars, and scans the returned suffix
    list for a match. Tells us how many breach corpora contain the
    password.
  * ``haveibeenpwned.com/api/v3/breaches`` — global breach metadata
    (name, domain, PwnCount, date). No key required. Enough to detect
    whether an email's *domain* is a known-breached service (weak
    signal but useful for ensemble scoring).

All lookups are cached in-process. Controlled via
``AFDS_ENABLE_HIBP`` (default 1).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 4.0
_PWD_CACHE: dict[str, tuple[float, int]] = {}
_PWD_CACHE_TTL = 30 * 60

_BREACH_DOMAINS: dict[str, list[dict[str, Any]]] | None = None
_BREACH_REFRESHED_AT: float = 0.0
_BREACH_TTL = 24 * 3600
_BREACH_LOCK = asyncio.Lock()


async def check_password_pwned(password: str) -> dict[str, Any]:
    """Pwned Passwords k-anonymity lookup.

    Returns a dict with ``pwned`` bool and ``count`` int (number of
    breach-corpora in which this password has been seen).
    """
    if not password:
        return {"pwned": False, "count": 0}
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    cached = _PWD_CACHE.get(sha1)
    if cached and (time.time() - cached[0]) < _PWD_CACHE_TTL:
        count = cached[1]
        return {"pwned": count > 0, "count": count}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"https://api.pwnedpasswords.com/range/{prefix}",
                headers={"Add-Padding": "true", "User-Agent": "AFDS/1.0"},
            )
        if resp.status_code != 200:
            return {"pwned": False, "count": 0, "error": f"status={resp.status_code}"}
        count = 0
        for line in resp.text.splitlines():
            parts = line.strip().split(":")
            if len(parts) == 2 and parts[0] == suffix:
                try:
                    count = int(parts[1])
                except ValueError:
                    count = 1
                break
        _PWD_CACHE[sha1] = (time.time(), count)
        return {"pwned": count > 0, "count": count}
    except Exception as exc:  # noqa: BLE001
        logger.debug("pwned passwords lookup failed: %s", exc)
        return {"pwned": False, "count": 0, "error": str(exc)[:120]}


async def _refresh_breach_metadata() -> None:
    global _BREACH_DOMAINS, _BREACH_REFRESHED_AT
    async with _BREACH_LOCK:
        if _BREACH_DOMAINS is not None and (time.time() - _BREACH_REFRESHED_AT) < _BREACH_TTL:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://haveibeenpwned.com/api/v3/breaches",
                    headers={"User-Agent": "AFDS/1.0"},
                )
            if resp.status_code != 200:
                logger.warning("HIBP breaches fetch failed: %s", resp.status_code)
                return
            data = resp.json()
            grouped: dict[str, list[dict[str, Any]]] = {}
            for item in data:
                domain = (item.get("Domain") or "").strip().lower()
                if not domain:
                    continue
                grouped.setdefault(domain, []).append({
                    "name": item.get("Name"),
                    "breach_date": item.get("BreachDate"),
                    "pwn_count": item.get("PwnCount"),
                    "data_classes": item.get("DataClasses") or [],
                })
            _BREACH_DOMAINS = grouped
            _BREACH_REFRESHED_AT = time.time()
            logger.info("Loaded HIBP breach metadata for %d domains", len(grouped))
        except Exception as exc:  # noqa: BLE001
            logger.debug("HIBP breach metadata refresh failed: %s", exc)


async def check_domain_in_breaches(domain: str) -> dict[str, Any]:
    """Return all known breaches whose Domain matches the given domain."""
    if not domain:
        return {"hit": False, "breaches": []}
    await _refresh_breach_metadata()
    if _BREACH_DOMAINS is None:
        return {"hit": False, "breaches": [], "source": "unavailable"}
    breaches = _BREACH_DOMAINS.get(domain.lower()) or []
    total_pwn = sum(b.get("pwn_count", 0) or 0 for b in breaches)
    return {
        "hit": bool(breaches),
        "breach_count": len(breaches),
        "total_accounts_exposed": total_pwn,
        "breaches": breaches[:5],
        "source": "hibp",
    }


def derive_email_risk(breach_info: dict[str, Any]) -> dict[str, Any]:
    factors: list[str] = []
    score = 0
    if breach_info.get("hit"):
        factors.append("domain_in_known_breach")
        bc = breach_info.get("breach_count", 0) or 0
        if bc >= 3:
            factors.append("domain_in_multiple_breaches")
            score += 5
    return {"risk_factors": factors, "risk_score": score}
