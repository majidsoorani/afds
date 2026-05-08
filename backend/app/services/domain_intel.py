"""
Domain intelligence — WHOIS + DNS (MX/SPF/DMARC) signals.

Free / offline-friendly replacement for third-party vendor's `domain_details` block. Uses:
  - python-whois: WHOIS registration data (created/updated/expires, registrar)
  - dnspython: MX / TXT (SPF) / _dmarc TXT lookups

All lookups are cached in-process for 24 h to avoid hammering WHOIS servers
(which aggressively rate-limit) and public DNS resolvers.

Usage:
    from app.services.domain_intel import get_domain_intel
    intel = await get_domain_intel("hotmail.com")
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h


def _coerce_date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _domain_age_days(created: Any) -> int | None:
    if not created:
        return None
    if isinstance(created, list):
        created = created[0] if created else None
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return None
    if not isinstance(created, datetime):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - created).total_seconds() // 86400))


def _sync_whois(domain: str) -> dict[str, Any]:
    try:
        import whois  # type: ignore
    except ImportError:
        return {"error": "python-whois not installed"}
    try:
        w = whois.whois(domain)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"whois_failed: {exc}"}
    created = getattr(w, "creation_date", None)
    updated = getattr(w, "updated_date", None)
    expires = getattr(w, "expiration_date", None)
    registrar = getattr(w, "registrar", None)
    return {
        "created": _coerce_date(created),
        "updated": _coerce_date(updated),
        "expires": _coerce_date(expires),
        "registrar": registrar if isinstance(registrar, str) else (registrar[0] if isinstance(registrar, list) and registrar else None),
        "domain_age_days": _domain_age_days(created),
        "registered": bool(created),
    }


def _sync_dns(domain: str) -> dict[str, Any]:
    try:
        import dns.resolver  # type: ignore
    except ImportError:
        return {"error": "dnspython not installed"}

    resolver = dns.resolver.Resolver()
    resolver.timeout = 3.0
    resolver.lifetime = 4.0

    def _txt(name: str) -> list[str]:
        try:
            answers = resolver.resolve(name, "TXT", raise_on_no_answer=False)
            out: list[str] = []
            for rdata in answers:
                # join chunked TXT pieces
                parts = [p.decode() if isinstance(p, bytes) else str(p) for p in rdata.strings]
                out.append("".join(parts))
            return out
        except Exception:
            return []

    def _mx(name: str) -> list[str]:
        try:
            answers = resolver.resolve(name, "MX", raise_on_no_answer=False)
            return [str(r.exchange).rstrip(".") for r in answers]
        except Exception:
            return []

    mx_records = _mx(domain)
    spf_records = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    dmarc_records = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]

    spf_strict = any("-all" in r.lower() for r in spf_records)
    dmarc_policy = None
    for r in dmarc_records:
        for part in r.split(";"):
            part = part.strip().lower()
            if part.startswith("p="):
                dmarc_policy = part.split("=", 1)[1]
                break
        if dmarc_policy:
            break
    dmarc_enforced = dmarc_policy in {"quarantine", "reject"}

    return {
        "mx_records": mx_records,
        "valid_mx": bool(mx_records),
        "spf_records": spf_records,
        "spf_strict": spf_strict,
        "dmarc_records": dmarc_records,
        "dmarc_policy": dmarc_policy,
        "dmarc_enforced": dmarc_enforced,
    }


async def get_domain_intel(domain: str, *, whois_timeout: float = 3.0) -> dict[str, Any]:
    """Async wrapper that fans out WHOIS + DNS probes and caches the result.

    WHOIS is bounded by ``whois_timeout`` seconds; if it exceeds that, the
    WHOIS block is empty but DNS results are still returned.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {}

    now = time.time()
    cached = _CACHE.get(domain)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    loop = asyncio.get_running_loop()
    # DNS first — cheap and gates WHOIS (skip WHOIS if domain has no MX at all)
    dns_data = await loop.run_in_executor(None, _sync_dns, domain)

    whois_data: dict[str, Any] = {}
    if dns_data.get("valid_mx") or dns_data.get("spf_records") or dns_data.get("dmarc_records"):
        try:
            whois_data = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_whois, domain),
                timeout=whois_timeout,
            )
        except asyncio.TimeoutError:
            whois_data = {"error": "whois_timeout"}
        except Exception as exc:  # noqa: BLE001
            whois_data = {"error": f"whois_exc: {exc}"}
    else:
        whois_data = {"error": "skipped_no_dns"}

    intel: dict[str, Any] = {
        "domain": domain,
        "whois": whois_data,
        "dns": dns_data,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    _CACHE[domain] = (now, intel)
    return intel


def summarize_for_email(intel: dict[str, Any]) -> dict[str, Any]:
    """Flatten to the signals the email enrichment exposes."""
    whois = intel.get("whois", {}) or {}
    dns = intel.get("dns", {}) or {}
    age_days = whois.get("domain_age_days")
    if age_days is None:
        domain_age_risk = "UNKNOWN"
    elif age_days < 30:
        domain_age_risk = "VERY_YOUNG"
    elif age_days < 180:
        domain_age_risk = "YOUNG"
    elif age_days < 365:
        domain_age_risk = "RECENT"
    else:
        domain_age_risk = "ESTABLISHED"

    return {
        "domain_created": whois.get("created"),
        "domain_updated": whois.get("updated"),
        "domain_expires": whois.get("expires"),
        "domain_registrar": whois.get("registrar"),
        "domain_age_days": age_days,
        "domain_age_risk": domain_age_risk,
        "valid_mx": dns.get("valid_mx"),
        "spf_strict": dns.get("spf_strict"),
        "dmarc_enforced": dns.get("dmarc_enforced"),
        "dmarc_policy": dns.get("dmarc_policy"),
    }
