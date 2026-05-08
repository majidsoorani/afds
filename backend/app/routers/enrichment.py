"""
OSINT Enrichment — Email, Phone, IP intelligence lookups.

Replicates third-party vendor's Digital Footprinting layer using open-source tools:
  - Email: breach check, social media presence, disposable email detection
  - IP: geolocation, VPN/proxy/Tor detection, datacenter classification
  - Phone: carrier lookup, line type, country validation

All lookups are async and results are cached in PostgreSQL for re-use.

Endpoints:
  POST /api/v1/enrichment/email      — Enrich an email address
  POST /api/v1/enrichment/ip         — Enrich an IP address
  POST /api/v1/enrichment/phone      — Enrich a phone number
  POST /api/v1/enrichment/transaction — Full enrichment for a transaction
  GET  /api/v1/enrichment/entity/{id} — Cached enrichment results for an entity
"""

from __future__ import annotations
import hashlib
import ipaddress
import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/enrichment", tags=["OSINT Enrichment"])


async def _get_db_optional():
    """Yield a DB session if available, else None."""
    try:
        async for session in get_db():
            yield session
            return
    except Exception:
        yield None

settings = get_settings()
SCHEMA = settings.postgres_schema

# Timeout for external lookups
LOOKUP_TIMEOUT = 5.0


# ── Schemas ──────────────────────────────────────────────────────────

class EmailEnrichRequest(BaseModel):
    email: str = Field(..., max_length=320)
    entity_id: str = Field(default="", max_length=255)
    known_breaches: int = Field(default=0, ge=0, description="Offline breach aggregation match.")
    registered_social_profiles: int = Field(default=-1, description="Offline OSINT footprint presence count (-1 indicates unknown).")


class IPEnrichRequest(BaseModel):
    ip_address: str = Field(..., max_length=45)
    entity_id: str = Field(default="", max_length=255)


class PhoneEnrichRequest(BaseModel):
    phone: str = Field(..., max_length=20)
    country_code: str = Field(default="", max_length=3)
    entity_id: str = Field(default="", max_length=255)


class BinEnrichRequest(BaseModel):
    bin: str = Field(..., max_length=19, description="Card BIN (first 6-8 digits) or full PAN — only the first 6 digits are used.")
    entity_id: str = Field(default="", max_length=255)
    expected_country: str = Field(default="", max_length=2, description="Optional ISO alpha-2 of expected issuing country; enables card_country_mismatch signal.")


class PasswordCheckRequest(BaseModel):
    password: str = Field(..., min_length=1, max_length=256, description="Plaintext password — hashed client-side with SHA1, only first 5 hex chars are sent to HIBP (k-anonymity).")
    entity_id: str = Field(default="", max_length=255)


class TransactionEnrichRequest(BaseModel):
    transaction_id: str
    sender_email: str | None = None
    sender_phone: str | None = None
    sender_ip: str | None = None
    receiver_email: str | None = None
    card_bin: str | None = Field(default=None, description="Gap 7: first 6-8 digits of the card PAN.")
    expected_country: str | None = Field(default=None, max_length=2, description="Gap 8: ISO alpha-2 of customer's registered country for geo cross-check.")


# ── Disposable Email Domains ────────────────────────────────────────
#
# Loaded at import time from two vendored community-maintained CC0 lists:
#   - backend/app/data/disposable_email_domains.txt       (~5k, disposable-email-domains/disposable-email-domains)
#   - backend/app/data/disposable_email_domains_large.txt (~72k, disposable/disposable-email-domains)
# Falls back to a small curated set if the files are missing.

_CURATED_DISPOSABLE = {
    "tempmail.com", "throwaway.email", "guerrillamail.com", "mailinator.com",
    "10minutemail.com", "yopmail.com", "trashmail.com", "sharklasers.com",
    "guerrillamailblock.com", "grr.la", "dispostable.com", "mailnesia.com",
    "tempr.email", "fakeinbox.com", "emailondeck.com", "getnada.com",
    "temp-mail.org", "mohmal.com", "tempail.com", "burnermail.io",
    "maildrop.cc", "harakirimail.com", "tmail.ws", "tmpmail.net",
}


def _load_disposable_domains() -> frozenset[str]:
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.normpath(os.path.join(here, "..", "data"))
    combined: set[str] = set(_CURATED_DISPOSABLE)
    for name in ("disposable_email_domains.txt", "disposable_email_domains_large.txt"):
        path = os.path.join(data_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    d = line.strip().lower()
                    if d and not d.startswith("#"):
                        combined.add(d)
        except FileNotFoundError:
            logger.info("Disposable domain list not vendored: %s", path)
    logger.info("Loaded %d disposable email domains", len(combined))
    return frozenset(combined)


DISPOSABLE_DOMAINS: frozenset[str] = _load_disposable_domains()

# Free email providers (not necessarily suspicious but worth noting)
FREE_EMAIL_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "gmx.com",
    "yandex.com", "tutanota.com", "fastmail.com",
}

# Known datacenter / cloud IP ranges (CIDR prefixes)
DATACENTER_PREFIXES = [
    "34.", "35.", "52.", "54.",     # AWS
    "104.16.", "104.17.",           # Cloudflare
    "13.64.", "13.65.", "20.33.",   # Azure
    "35.186.", "35.187.",           # GCP
    "159.203.", "167.99.",          # DigitalOcean
    "5.188.", "45.155.",            # Known proxy ranges
]

# ── Email Analysis ───────────────────────────────────────────────────

def _analyze_email(email: str, known_breaches: int = 0, registered_social_profiles: int = -1) -> dict:
    """Offline email intelligence without external API calls."""
    email = email.lower().strip()
    signals: dict = {
        "email": email,
        "valid_format": False,
        "domain": "",
        "is_disposable": False,
        "is_free_provider": False,
        "has_plus_alias": False,
        "has_dots_trick": False,
        "domain_age_risk": "UNKNOWN",
        "social_profiles_estimated": registered_social_profiles if registered_social_profiles > -1 else 0,
        "known_breaches": known_breaches,
        "risk_score": 0,
        "risk_factors": [],
    }

    # Format validation
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, email):
        signals["risk_factors"].append("invalid_email_format")
        signals["risk_score"] = 40
        return signals

    signals["valid_format"] = True
    local, domain = email.rsplit("@", 1)
    signals["domain"] = domain

    # Disposable email check
    if domain in DISPOSABLE_DOMAINS:
        signals["is_disposable"] = True
        signals["risk_factors"].append("disposable_email")
        signals["risk_score"] += 35

    # Free provider
    if domain in FREE_EMAIL_PROVIDERS:
        signals["is_free_provider"] = True
        signals["risk_factors"].append("free_email_provider")
        signals["risk_score"] += 5

    # Plus alias (user+tag@gmail.com — obfuscation technique)
    if "+" in local:
        signals["has_plus_alias"] = True
        signals["risk_factors"].append("plus_alias_detected")
        signals["risk_score"] += 10

    # Dots trick (Gmail ignores dots: j.o.h.n = john)
    if domain == "gmail.com" and "." in local:
        signals["has_dots_trick"] = True

    # Estimate social media presence (heuristic: non-disposable + real domain = higher)
    if registered_social_profiles == -1:
        if not signals["is_disposable"] and signals["valid_format"]:
            signals["social_profiles_estimated"] = 3 if not signals["is_free_provider"] else 5
    else:
        # third-party vendor Parity: Explicit footprint tracking
        if registered_social_profiles == 0 and signals["is_free_provider"]:
            signals["risk_factors"].append("BURNER_EMAIL_NO_SOCIAL")
            signals["risk_score"] += 30
            
    # third-party vendor Parity: Breach Context
    if known_breaches >= 10:
        signals["risk_factors"].append("MASS_COMPROMISED_EMAIL_BREACHES")
        signals["risk_score"] += 25
    elif known_breaches >= 1:
        signals["risk_factors"].append("COMPROMISED_EMAIL_BREACH")
        signals["risk_score"] += 5

    # Short local part (often auto-generated)
    if len(local) <= 3:
        signals["risk_factors"].append("very_short_local_part")
        signals["risk_score"] += 8

    # Numeric-heavy local part
    digits = sum(1 for c in local if c.isdigit())
    if digits > len(local) * 0.6:
        signals["risk_factors"].append("numeric_heavy_email")
        signals["risk_score"] += 10

    signals["risk_score"] = min(signals["risk_score"], 100)
    return signals


# ── IP Analysis ──────────────────────────────────────────────────────

def _analyze_ip(ip_str: str) -> dict:
    """Offline IP intelligence analysis."""
    signals: dict = {
        "ip_address": ip_str,
        "valid": False,
        "version": None,
        "is_private": False,
        "is_loopback": False,
        "is_datacenter": False,
        "is_vpn_likely": False,
        "is_tor_exit": False,
        "is_proxy_likely": False,
        "geolocation": None,
        "risk_score": 0,
        "risk_factors": [],
    }

    try:
        ip = ipaddress.ip_address(ip_str)
        signals["valid"] = True
        signals["version"] = ip.version
        signals["is_private"] = ip.is_private
        signals["is_loopback"] = ip.is_loopback
    except ValueError:
        signals["risk_factors"].append("invalid_ip_address")
        signals["risk_score"] = 30
        return signals

    if ip.is_private or ip.is_loopback:
        signals["risk_factors"].append("private_or_loopback_ip")
        signals["risk_score"] += 15

    # Datacenter / cloud IP heuristic
    for prefix in DATACENTER_PREFIXES:
        if ip_str.startswith(prefix):
            signals["is_datacenter"] = True
            signals["is_vpn_likely"] = True
            signals["risk_factors"].append("datacenter_ip_detected")
            signals["risk_score"] += 25
            break

    # IPv6 (less common for consumer — might indicate proxy)
    if ip.version == 6:
        signals["risk_factors"].append("ipv6_address")
        signals["risk_score"] += 5

    # Gap 5: extended IP intel (VPN / Tor / datacenter / ASN) from vendored lists
    if os.getenv("AFDS_ENABLE_IP_INTEL", "1") == "1":
        try:
            from app.services.ip_intel import analyze_ip_extended
            extra = analyze_ip_extended(ip_str)
            signals.update({
                "asn": extra.get("asn"),
                "asn_org": extra.get("asn_org"),
                "country_code": extra.get("country"),
            })
            if extra.get("is_vpn") and not signals["is_vpn_likely"]:
                signals["is_vpn_likely"] = True
                signals["risk_factors"].append("vpn_cidr_match")
                signals["risk_score"] += 25
            if extra.get("is_datacenter_cidr") and not signals["is_datacenter"]:
                signals["is_datacenter"] = True
                signals["is_vpn_likely"] = True
                signals["risk_factors"].append("datacenter_cidr_match")
                signals["risk_score"] += 25
            if extra.get("is_tor_exit"):
                signals["is_tor_exit"] = True
                signals["risk_factors"].append("tor_exit_node")
                signals["risk_score"] += 40
        except Exception as exc:  # noqa: BLE001
            logger.debug("IP intel lookup failed for %s: %s", ip_str, exc)

    signals["risk_score"] = min(signals["risk_score"], 100)
    return signals


# ── Phone Analysis ───────────────────────────────────────────────────

def _analyze_phone(phone: str, country_code: str = "") -> dict:
    """Offline phone number intelligence."""
    signals: dict = {
        "phone": phone,
        "valid_format": False,
        "country_code": country_code,
        "is_voip_likely": False,
        "is_prepaid_likely": False,
        "carrier": "UNKNOWN",
        "line_type": "UNKNOWN",
        "risk_score": 0,
        "risk_factors": [],
    }

    # Basic format validation
    cleaned = re.sub(r"[^\d+]", "", phone)
    if len(cleaned) < 7 or len(cleaned) > 15:
        signals["risk_factors"].append("invalid_phone_length")
        signals["risk_score"] = 25
        return signals

    signals["valid_format"] = True

    # VoIP prefix heuristics (US-centric; extend per country)
    voip_prefixes = ["1900", "1800", "1888", "1877", "1866"]
    for prefix in voip_prefixes:
        if cleaned.startswith(prefix):
            signals["is_voip_likely"] = True
            signals["risk_factors"].append("voip_number_detected")
            signals["risk_score"] += 20
            break

    # Premium rate numbers
    if cleaned.startswith("190") or cleaned.startswith("0900"):
        signals["risk_factors"].append("premium_rate_number")
        signals["risk_score"] += 15

    # Gap 6: phonenumbers library — canonical validation, line type, carrier, country
    if os.getenv("AFDS_ENABLE_PHONE_INTEL", "1") == "1":
        try:
            import phonenumbers
            from phonenumbers import carrier as pn_carrier, geocoder as pn_geocoder, PhoneNumberType
            default_region = (country_code or "US").upper() if country_code else None
            parsed = phonenumbers.parse(phone, default_region)
            if phonenumbers.is_valid_number(parsed):
                signals["valid_format"] = True
                signals["country_code"] = f"+{parsed.country_code}"
                signals["country_name"] = pn_geocoder.description_for_number(parsed, "en") or None
                num_type = phonenumbers.number_type(parsed)
                type_map = {
                    PhoneNumberType.FIXED_LINE: "FIXED_LINE",
                    PhoneNumberType.MOBILE: "MOBILE",
                    PhoneNumberType.FIXED_LINE_OR_MOBILE: "FIXED_OR_MOBILE",
                    PhoneNumberType.TOLL_FREE: "TOLL_FREE",
                    PhoneNumberType.PREMIUM_RATE: "PREMIUM_RATE",
                    PhoneNumberType.SHARED_COST: "SHARED_COST",
                    PhoneNumberType.VOIP: "VOIP",
                    PhoneNumberType.PERSONAL_NUMBER: "PERSONAL",
                    PhoneNumberType.PAGER: "PAGER",
                    PhoneNumberType.UAN: "UAN",
                    PhoneNumberType.UNKNOWN: "UNKNOWN",
                }
                signals["line_type"] = type_map.get(num_type, "UNKNOWN")
                carrier_name = pn_carrier.name_for_number(parsed, "en") or "UNKNOWN"
                signals["carrier"] = carrier_name
                if num_type == PhoneNumberType.VOIP and not signals["is_voip_likely"]:
                    signals["is_voip_likely"] = True
                    signals["risk_factors"].append("voip_number_detected")
                    signals["risk_score"] += 20
                if num_type == PhoneNumberType.PREMIUM_RATE and "premium_rate_number" not in signals["risk_factors"]:
                    signals["risk_factors"].append("premium_rate_number")
                    signals["risk_score"] += 15
            else:
                # phonenumbers considers it invalid. Only penalise if the
                # number isn't even a possible format — otherwise reserved
                # test ranges (e.g. Ofcom UK 07700 900xxx) would be flagged.
                if not phonenumbers.is_possible_number(parsed):
                    if "invalid_phone_length" not in signals["risk_factors"]:
                        signals["risk_factors"].append("phonenumbers_invalid")
                        signals["risk_score"] += 15
        except Exception as exc:  # noqa: BLE001
            logger.debug("phonenumbers parse failed for %s: %s", phone, exc)

    signals["risk_score"] = min(signals["risk_score"], 100)
    return signals


# ── Persist enrichment result ────────────────────────────────────────

async def _save_enrichment(db: AsyncSession, entity_id: str, enrich_type: str, data: dict):
    """Cache enrichment result in DB."""
    if not entity_id:
        return
    risk_score = data.get("risk_score", 0)
    await db.execute(text(f"""
        INSERT INTO {SCHEMA}.enrichment_results
            (entity_id, enrichment_type, data, risk_score, created_at)
        VALUES
            (:entity_id, :etype, :data, :risk_score, NOW())
    """), {
        "entity_id": entity_id,
        "etype": enrich_type,
        "data": json.dumps(data, default=str),
        "risk_score": risk_score,
    })
    await db.commit()


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/email")
async def enrich_email(body: EmailEnrichRequest, db: AsyncSession | None = Depends(_get_db_optional)):
    """Analyze an email address for fraud signals."""
    signals = _analyze_email(body.email, body.known_breaches, body.registered_social_profiles)
    # Gap 3: live domain intel (WHOIS + DMARC/SPF/MX) — only for valid, non-disposable format
    # Gated by env var so parity harness / unit tests can skip network calls.
    enable_domain_intel = os.getenv("AFDS_ENABLE_DOMAIN_INTEL", "1") == "1"
    if enable_domain_intel and signals.get("valid_format") and signals.get("domain"):
        try:
            from app.services.domain_intel import get_domain_intel, summarize_for_email
            intel = await get_domain_intel(signals["domain"])
            summary = summarize_for_email(intel)
            signals.update(summary)
            # Surface "very young domain" as an additional risk factor
            if summary.get("domain_age_risk") == "VERY_YOUNG":
                signals["risk_factors"].append("very_young_domain")
                signals["risk_score"] = min(signals["risk_score"] + 20, 100)
            elif summary.get("domain_age_risk") == "YOUNG":
                signals["risk_factors"].append("young_domain")
                signals["risk_score"] = min(signals["risk_score"] + 10, 100)
            # No MX = undeliverable domain
            if summary.get("valid_mx") is False:
                signals["risk_factors"].append("no_mx_records")
                signals["risk_score"] = min(signals["risk_score"] + 15, 100)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Domain intel lookup failed for %s: %s", signals.get("domain"), exc)
    # Gap 2: per-platform social footprint via holehe
    if os.getenv("AFDS_ENABLE_SOCIAL_INTEL", "1") == "1" and signals.get("valid_format"):
        try:
            from app.services.social_intel import check_email_social, derive_risk_signals as _social_risk
            social = await check_email_social(body.email)
            srisk = _social_risk(social)
            signals["social_footprint"] = social
            signals["social_platforms_registered"] = srisk["social_platforms_registered"]
            # Suppress the "no footprint" signal for well-established corporate
            # domains — consumer social presence is not a useful signal there.
            is_established = signals.get("domain_age_risk") == "ESTABLISHED" or \
                             (signals.get("domain_age_days") or 0) >= 3650
            for f in srisk["social_risk_factors"]:
                if f == "no_social_footprint" and is_established:
                    continue
                if f not in signals["risk_factors"]:
                    signals["risk_factors"].append(f)
                    signals["risk_score"] = min(signals["risk_score"] + srisk["social_risk_score"], 100)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Social intel failed for %s: %s", body.email, exc)
    # Gap 1: HIBP free-tier domain-in-breach check
    if os.getenv("AFDS_ENABLE_HIBP", "1") == "1" and signals.get("domain"):
        try:
            from app.services.hibp_intel import check_domain_in_breaches, derive_email_risk as _hibp_risk
            info = await check_domain_in_breaches(signals["domain"])
            signals["hibp"] = info
            hrisk = _hibp_risk(info)
            for f in hrisk["risk_factors"]:
                if f not in signals["risk_factors"]:
                    signals["risk_factors"].append(f)
            signals["risk_score"] = min(signals["risk_score"] + hrisk["risk_score"], 100)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HIBP lookup failed for %s: %s", signals.get("domain"), exc)
    if db:
        try:
            await _save_enrichment(db, body.entity_id, "email", signals)
        except Exception:
            logger.debug("Could not save enrichment to DB")
    return signals


@router.post("/ip")
async def enrich_ip(body: IPEnrichRequest, db: AsyncSession | None = Depends(_get_db_optional)):
    """Analyze an IP address for VPN/proxy/datacenter signals."""
    signals = _analyze_ip(body.ip_address)
    if db:
        try:
            await _save_enrichment(db, body.entity_id, "ip", signals)
        except Exception:
            logger.debug("Could not save enrichment to DB")
    return signals


@router.post("/phone")
async def enrich_phone(body: PhoneEnrichRequest, db: AsyncSession | None = Depends(_get_db_optional)):
    """Analyze a phone number for VoIP/prepaid signals."""
    signals = _analyze_phone(body.phone, body.country_code)
    if db:
        try:
            await _save_enrichment(db, body.entity_id, "phone", signals)
        except Exception:
            logger.debug("Could not save enrichment to DB")
    return signals


@router.post("/bin")
async def enrich_bin(body: BinEnrichRequest, db: AsyncSession | None = Depends(_get_db_optional)):
    """Gap 7: BIN lookup — scheme/type/brand/issuer country/bank.

    Combines offline ISO/IEC 7812 prefix classification with an optional
    live ``binlist.net`` lookup (gated by ``AFDS_ENABLE_BIN_LIVE``).
    Results are cached in-process for 24h.
    """
    from app.services.bin_intel import lookup_bin, derive_risk_signals
    intel = await lookup_bin(body.bin)
    expected = body.expected_country.strip().upper() or None
    risk = derive_risk_signals(intel, expected_country=expected)
    result = {**intel, **risk}
    if db:
        try:
            await _save_enrichment(db, body.entity_id, "bin", result)
        except Exception:
            logger.debug("Could not save BIN enrichment to DB")
    return result


@router.get("/bin/{bin_value}")
async def enrich_bin_get(bin_value: str, expected_country: str = ""):
    """Convenience GET variant for ad-hoc BIN lookups."""
    from app.services.bin_intel import lookup_bin, derive_risk_signals
    intel = await lookup_bin(bin_value)
    risk = derive_risk_signals(intel, expected_country=(expected_country.strip().upper() or None))
    return {**intel, **risk}


@router.post("/password")
async def enrich_password(body: PasswordCheckRequest):
    """Gap 1: HIBP Pwned Passwords k-anonymity breach check.

    Only the first 5 chars of the SHA-1 hash are sent to HIBP. The full
    password never leaves the AFDS backend. Returns the corpus match
    count (``count``) — >0 means the password has leaked in at least
    one breach.
    """
    from app.services.hibp_intel import check_password_pwned
    result = await check_password_pwned(body.password)
    result["risk_factors"] = []
    count = result.get("count", 0) or 0
    if count >= 1000:
        result["risk_factors"].append("password_mass_breached")
        result["risk_score"] = 40
    elif count > 0:
        result["risk_factors"].append("password_breached")
        result["risk_score"] = 25
    else:
        result["risk_score"] = 0
    return result


@router.post("/transaction")
async def enrich_transaction(body: TransactionEnrichRequest, db: AsyncSession | None = Depends(_get_db_optional)):
    """Full enrichment suite for a transaction — combines all available signals."""
    results: dict = {"transaction_id": body.transaction_id, "enrichments": {}, "combined_risk_score": 0}
    total_score = 0.0
    count = 0

    if body.sender_email:
        signals = _analyze_email(body.sender_email)
        results["enrichments"]["sender_email"] = signals
        total_score += signals["risk_score"]
        count += 1
        if db:
            try:
                await _save_enrichment(db, body.transaction_id, "sender_email", signals)
            except Exception:
                pass

    if body.sender_ip:
        signals = _analyze_ip(body.sender_ip)
        results["enrichments"]["sender_ip"] = signals
        total_score += signals["risk_score"]
        count += 1
        if db:
            try:
                await _save_enrichment(db, body.transaction_id, "sender_ip", signals)
            except Exception:
                pass

    if body.sender_phone:
        signals = _analyze_phone(body.sender_phone)
        results["enrichments"]["sender_phone"] = signals
        total_score += signals["risk_score"]
        count += 1
        if db:
            try:
                await _save_enrichment(db, body.transaction_id, "sender_phone", signals)
            except Exception:
                pass

    if body.receiver_email:
        signals = _analyze_email(body.receiver_email)
        results["enrichments"]["receiver_email"] = signals
        total_score += signals["risk_score"]
        count += 1
        if db:
            try:
                await _save_enrichment(db, body.transaction_id, "receiver_email", signals)
            except Exception:
                pass

    # Gap 7: card BIN enrichment
    if body.card_bin:
        try:
            from app.services.bin_intel import lookup_bin, derive_risk_signals
            intel = await lookup_bin(body.card_bin)
            bin_signals = {**intel, **derive_risk_signals(intel, expected_country=body.expected_country)}
            results["enrichments"]["card_bin"] = bin_signals
            total_score += bin_signals.get("risk_score", 0)
            count += 1
            if db:
                try:
                    await _save_enrichment(db, body.transaction_id, "card_bin", bin_signals)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("BIN enrichment failed: %s", exc)

    # Gap 8: cross-signal geo correlation ─────────────────────────────
    # Collect ISO country codes derived from each enrichment and flag
    # any disagreement. A mismatch between the IP geolocation, phone
    # country and card-issuing country is one of the strongest fraud
    # signals third-party vendor reports.
    enr = results["enrichments"]
    geo_sources: dict[str, str] = {}
    ip_cc = (enr.get("sender_ip") or {}).get("country_code")
    if ip_cc:
        geo_sources["ip"] = str(ip_cc).upper()
    phone_cc = (enr.get("sender_phone") or {}).get("country_name") and \
               (enr.get("sender_phone") or {}).get("country_code")
    # phone country_code from phonenumbers is e.g. "+44" — convert via region lookup
    phone_region = None
    if enr.get("sender_phone"):
        try:
            import phonenumbers
            parsed = phonenumbers.parse((enr["sender_phone"]).get("phone") or body.sender_phone, None)
            phone_region = phonenumbers.region_code_for_number(parsed)
        except Exception:
            phone_region = None
    if phone_region:
        geo_sources["phone"] = phone_region.upper()
    bin_cc = (enr.get("card_bin") or {}).get("issuer_country") or \
             (enr.get("card_bin") or {}).get("country_code")
    if bin_cc:
        geo_sources["card"] = str(bin_cc).upper()
    if body.expected_country:
        geo_sources["expected"] = body.expected_country.upper()

    geo_check: dict = {
        "sources": geo_sources,
        "risk_factors": [],
        "risk_score": 0,
    }
    distinct = {v for v in geo_sources.values() if v}
    if len(distinct) >= 2:
        geo_check["risk_factors"].append("geo_mismatch")
        geo_check["risk_score"] = 25 + 10 * (len(distinct) - 2)
        if body.expected_country and body.expected_country.upper() not in distinct.intersection({geo_sources.get("ip"), geo_sources.get("phone"), geo_sources.get("card")}):
            # expected country not represented in any derived source
            geo_check["risk_factors"].append("expected_country_absent")
            geo_check["risk_score"] += 10
    results["enrichments"]["geo_cross_check"] = geo_check
    if geo_check["risk_score"]:
        total_score += geo_check["risk_score"]
        count += 1

    results["combined_risk_score"] = round(total_score / max(count, 1), 1)
    return results


@router.get("/entity/{entity_id}")
async def get_entity_enrichments(entity_id: str, db: AsyncSession | None = Depends(_get_db_optional)):
    """Retrieve all cached enrichment results for an entity/transaction."""
    if not db:
        raise HTTPException(status_code=503, detail="Database not available")
    result = await db.execute(text(f"""
        SELECT enrichment_type, data, risk_score, created_at
        FROM {SCHEMA}.enrichment_results
        WHERE entity_id = :eid
        ORDER BY created_at DESC
    """), {"eid": entity_id})
    rows = [dict(r._mapping) for r in result.fetchall()]
    if not rows:
        raise HTTPException(status_code=404, detail="No enrichments found")

    return {
        "entity_id": entity_id,
        "enrichment_count": len(rows),
        "enrichments": rows,
    }
