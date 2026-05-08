"""
Social footprint intelligence (Gap 2) — holehe-based per-platform check.

Given an email, query a curated subset of ~15 high-signal holehe modules
concurrently to build a third-party vendor-style per-platform presence summary:

    {
        "platforms_checked": 15,
        "platforms_registered": 4,
        "registered": ["github", "spotify", "pinterest", "adobe"],
        "not_registered": [...],
        "rate_limited": [...],
        "breaches_rate_limited": 0,
    }

Gated by ``AFDS_ENABLE_SOCIAL_INTEL`` (default 1). Has an in-process 12h
cache keyed on email so repeated lookups during a session don't hit the
network.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Curated subset: fast modules, high real-world coverage, no CAPTCHA pages.
# (All ship with holehe 1.61.)
_CURATED = [
    "cms.gravatar",
    "cms.wordpress",
    "company.aboutme",
    "crm.nimble",
    "music.deezer",
    "music.lastfm",
    "music.spotify",
    "news.mediumcom",
    "payment.venmo",
    "shopping.ebay",
    "social_media.pinterest",
    "social_media.twitter",
    "social_media.vivino",
    "tech.adobe",
    "tech.github",
]

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 12 * 3600
_TIMEOUT = 6.0

_loaded_modules: list[tuple[str, Any]] | None = None


def _load_modules() -> list[tuple[str, Any]]:
    global _loaded_modules
    if _loaded_modules is not None:
        return _loaded_modules
    loaded: list[tuple[str, Any]] = []
    try:
        import importlib
        for path in _CURATED:
            leaf = path.rsplit(".", 1)[-1]
            try:
                mod = importlib.import_module(f"holehe.modules.{path}")
                fn = getattr(mod, leaf, None)
                if fn is not None:
                    loaded.append((leaf, fn))
            except Exception as exc:  # noqa: BLE001
                logger.debug("holehe import failed for %s: %s", path, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("holehe not available: %s", exc)
    logger.info("Loaded %d holehe modules", len(loaded))
    _loaded_modules = loaded
    return loaded


async def _run_one(name: str, fn, email: str, client: httpx.AsyncClient) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    try:
        await asyncio.wait_for(fn(email, client, out), timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("holehe %s failed: %s", name, exc)
        return {"name": name, "exists": None, "error": True}
    if out:
        return out[0]
    return {"name": name, "exists": None}


async def check_email_social(email: str) -> dict[str, Any]:
    """Run curated holehe modules against an email. Returns a summary dict
    with counts + per-platform lists. Always best-effort: network failures
    surface as ``exists=None``.
    """
    cached = _CACHE.get(email)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return dict(cached[1])

    modules = _load_modules()
    if not modules:
        return {
            "platforms_checked": 0,
            "platforms_registered": 0,
            "registered": [],
            "not_registered": [],
            "rate_limited": [],
            "source": "unavailable",
        }

    async with httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (AFDS-SocialIntel)"},
    ) as client:
        results = await asyncio.gather(
            *[_run_one(name, fn, email, client) for name, fn in modules],
            return_exceptions=False,
        )

    registered: list[str] = []
    not_registered: list[str] = []
    rate_limited: list[str] = []
    for r in results:
        pname = r.get("name")
        if r.get("rateLimit") or r.get("frequent_rate_limit"):
            rate_limited.append(pname)
        if r.get("exists") is True:
            registered.append(pname)
        elif r.get("exists") is False:
            not_registered.append(pname)

    summary: dict[str, Any] = {
        "platforms_checked": len(modules),
        "platforms_registered": len(registered),
        "registered": sorted(registered),
        "not_registered": sorted(not_registered),
        "rate_limited": sorted(set(rate_limited)),
        "source": "holehe",
    }

    # cheap in-process cache
    if len(_CACHE) > 2000:
        # drop oldest 10%
        victims = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:200]
        for k, _ in victims:
            _CACHE.pop(k, None)
    _CACHE[email] = (time.time(), dict(summary))
    return summary


def derive_risk_signals(summary: dict[str, Any]) -> dict[str, Any]:
    """Convert a social footprint summary to AFDS risk signals.

    third-party vendor treats *absence* of a social footprint as a weak fraud signal
    (fresh, unaged identity). Presence on many platforms is mildly
    de-risking. Rate-limited checks are ignored for scoring.

    Thresholds are conservative to avoid false positives on corporate
    /role addresses which rarely register on consumer platforms: the
    signal only fires when at least 7 platforms answered non-rate-
    limited and zero reported the address as registered.
    """
    risk_score = 0
    factors: list[str] = []
    n = summary.get("platforms_registered", 0)
    checked = summary.get("platforms_checked", 0)
    effective = checked - len(summary.get("rate_limited") or [])
    if effective >= 7 and n == 0:
        factors.append("no_social_footprint")
        risk_score += 10
    return {
        "social_risk_factors": factors,
        "social_risk_score": risk_score,
        "social_platforms_registered": n,
    }
