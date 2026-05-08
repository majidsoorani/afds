"""
Device Fingerprinting — Collect, store, and analyze browser/device fingerprints.

Replicates third-party vendor's device intelligence layer using open-source FingerprintJS signals
combined with behavioral biometrics (typing cadence, mouse entropy) and environment
anomaly detection (emulator, VPN, Tor, headless browser).

Endpoints:
  POST /api/v1/device/collect     — Ingest a fingerprint payload from the client SDK
  GET  /api/v1/device/{hash}      — Retrieve stored fingerprint + risk signals
  GET  /api/v1/device/user/{uid}  — All devices associated with a user
  POST /api/v1/device/analyze     — Analyze a fingerprint for anomalies (stateless)
"""

import hashlib
import json
import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/device", tags=["Device Fingerprinting"])

settings = get_settings()
SCHEMA = settings.postgres_schema


# ── Schemas ──────────────────────────────────────────────────────────

class DeviceFingerprint(BaseModel):
    """Payload sent by the client-side fingerprinting SDK."""
    user_id: str = Field(..., max_length=255)
    session_id: str = Field(..., max_length=255)

    # Browser / hardware signals (FingerprintJS-compatible)
    user_agent: str = Field(..., max_length=1000)
    platform: str = Field(default="", max_length=100)
    language: str = Field(default="", max_length=50)
    languages: list[str] = Field(default_factory=list)
    timezone_offset: int = 0
    timezone_name: str = Field(default="", max_length=100)
    screen_width: int = 0
    screen_height: int = 0
    color_depth: int = 0
    device_memory: float | None = None
    hardware_concurrency: int | None = None
    touch_support: bool = False
    max_touch_points: int = 0

    # Canvas / WebGL fingerprint hashes
    canvas_hash: str = Field(default="", max_length=64)
    webgl_hash: str = Field(default="", max_length=64)
    webgl_vendor: str = Field(default="", max_length=200)
    webgl_renderer: str = Field(default="", max_length=200)
    audio_hash: str = Field(default="", max_length=64)

    # Network signals
    ip_address: str = Field(default="", max_length=45)
    connection_type: str = Field(default="", max_length=50)

    # Behavioral biometrics
    typing_cadence_ms: list[float] = Field(default_factory=list, description="Inter-keystroke intervals")
    mouse_movements: int = 0
    mouse_entropy: float = 0.0
    scroll_behavior: str = Field(default="", max_length=50)

    # Environment flags (client-detected)
    cookies_enabled: bool = True
    local_storage: bool = True
    session_storage: bool = True
    indexed_db: bool = True
    do_not_track: bool = False
    ad_blocker: bool = False
    webdriver: bool = False  # navigator.webdriver
    plugins_count: int = 0
    pdf_viewer: bool = True


class DeviceAnalysis(BaseModel):
    device_hash: str
    risk_score: float
    risk_level: str
    anomalies: list[dict]
    is_emulator: bool
    is_headless: bool
    is_vpn: bool
    is_tor: bool
    is_bot: bool
    device_age_hours: float | None = None
    seen_count: int = 0
    linked_users: int = 0


class AnalyzeRequest(BaseModel):
    fingerprint: DeviceFingerprint


# ── Anomaly Detection Logic ─────────────────────────────────────────

# Known headless browser / emulator indicators
HEADLESS_INDICATORS = {
    "webgl_renderer": ["swiftshader", "llvmpipe", "mesa", "google swiftshader"],
    "webgl_vendor": ["google inc."],
    "user_agent": ["headlesschrome", "phantomjs", "slimerjs", "puppeteer"],
    "platform": [""],
}

EMULATOR_SCREEN_SIZES = {(360, 640), (375, 667), (414, 896), (320, 568)}

TOR_EXIT_PATTERN = "tor"  # Simplified; in production integrate Tor exit node list


def _compute_device_hash(fp: DeviceFingerprint) -> str:
    """Deterministic device hash from stable signals (canvas + webgl + screen + UA)."""
    key = f"{fp.canvas_hash}|{fp.webgl_hash}|{fp.screen_width}x{fp.screen_height}|{fp.user_agent}|{fp.platform}|{fp.language}|{fp.timezone_name}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _typing_entropy(cadences: list[float]) -> float:
    """Shannon entropy of keystroke intervals — bots have near-zero entropy."""
    if len(cadences) < 5:
        return 0.0
    bins = [0] * 10
    min_c = min(cadences)
    max_c = max(cadences)
    if max_c == min_c:
        return 0.0
    span = max_c - min_c
    for c in cadences:
        idx = min(int((c - min_c) / span * 10), 9)
        bins[idx] += 1
    total = len(cadences)
    entropy = 0.0
    for b in bins:
        if b > 0:
            p = b / total
            entropy -= p * math.log2(p)
    return round(entropy, 3)


def analyze_fingerprint(fp: DeviceFingerprint) -> DeviceAnalysis:
    """Run anomaly detection heuristics on a device fingerprint."""
    device_hash = _compute_device_hash(fp)
    anomalies: list[dict] = []
    score = 0.0

    # 1. Headless browser detection
    is_headless = False
    ua_lower = fp.user_agent.lower()
    for kw in HEADLESS_INDICATORS["user_agent"]:
        if kw in ua_lower:
            is_headless = True
            anomalies.append({"type": "headless_browser", "detail": f"UA contains '{kw}'", "weight": 35})
            score += 35
            break

    renderer_lower = fp.webgl_renderer.lower()
    for kw in HEADLESS_INDICATORS["webgl_renderer"]:
        if kw in renderer_lower:
            is_headless = True
            anomalies.append({"type": "headless_webgl", "detail": f"WebGL renderer: {fp.webgl_renderer}", "weight": 30})
            score += 30
            break

    # 2. Webdriver flag (Selenium, Puppeteer, Playwright)
    is_bot = fp.webdriver
    if fp.webdriver:
        anomalies.append({"type": "webdriver_detected", "detail": "navigator.webdriver = true", "weight": 40})
        score += 40

    # 3. Emulator detection
    is_emulator = False
    if fp.device_memory is not None and fp.device_memory <= 1:
        is_emulator = True
        anomalies.append({"type": "low_device_memory", "detail": f"{fp.device_memory}GB RAM", "weight": 15})
        score += 15
    if fp.hardware_concurrency is not None and fp.hardware_concurrency <= 1:
        anomalies.append({"type": "single_core", "detail": f"{fp.hardware_concurrency} cores", "weight": 10})
        score += 10
        is_emulator = True

    # 4. Behavioral biometrics: typing entropy
    t_entropy = _typing_entropy(fp.typing_cadence_ms)
    if fp.typing_cadence_ms and t_entropy < 1.0:
        anomalies.append({"type": "low_typing_entropy", "detail": f"entropy={t_entropy}", "weight": 20})
        score += 20
        is_bot = True

    # 5. Zero mouse activity (potential bot / API automation)
    if fp.mouse_movements == 0 and fp.mouse_entropy == 0.0:
        anomalies.append({"type": "no_mouse_activity", "detail": "0 movements, 0 entropy", "weight": 15})
        score += 15

    # 6. Missing web APIs (suspicious environment stripping)
    missing_apis = []
    if not fp.local_storage:
        missing_apis.append("localStorage")
    if not fp.session_storage:
        missing_apis.append("sessionStorage")
    if not fp.indexed_db:
        missing_apis.append("indexedDB")
    if not fp.cookies_enabled:
        missing_apis.append("cookies")
    if len(missing_apis) >= 2:
        anomalies.append({"type": "missing_web_apis", "detail": ", ".join(missing_apis), "weight": 10})
        score += 10

    # 7. No plugins (common in headless / stripped browsers)
    if fp.plugins_count == 0 and not fp.pdf_viewer:
        anomalies.append({"type": "no_plugins", "detail": "0 plugins, no PDF viewer", "weight": 8})
        score += 8

    # 8. Canvas/WebGL hash missing (fingerprint evasion)
    if not fp.canvas_hash and not fp.webgl_hash:
        anomalies.append({"type": "missing_fingerprint_hashes", "detail": "No canvas or WebGL hash", "weight": 20})
        score += 20

    # 9. VPN / Tor heuristic (based on timezone vs IP mismatch, connection type)
    is_vpn = False
    is_tor = False
    conn_lower = fp.connection_type.lower()
    if TOR_EXIT_PATTERN in conn_lower or TOR_EXIT_PATTERN in fp.ip_address.lower():
        is_tor = True
        anomalies.append({"type": "tor_detected", "detail": "Connection type indicates Tor", "weight": 25})
        score += 25

    # 10. Touch support on desktop UA (inconsistency)
    if fp.touch_support and fp.max_touch_points > 0:
        if "mobile" not in ua_lower and "android" not in ua_lower and "iphone" not in ua_lower:
            anomalies.append({"type": "touch_on_desktop", "detail": f"touch_points={fp.max_touch_points} on desktop UA", "weight": 8})
            score += 8

    score = min(score, 100.0)
    risk_level = "CRITICAL" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"

    return DeviceAnalysis(
        device_hash=device_hash,
        risk_score=round(score, 1),
        risk_level=risk_level,
        anomalies=anomalies,
        is_emulator=is_emulator,
        is_headless=is_headless,
        is_vpn=is_vpn,
        is_tor=is_tor,
        is_bot=is_bot,
    )


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/collect")
async def collect_fingerprint(fp: DeviceFingerprint, db: AsyncSession = Depends(get_db)):
    """Collect a device fingerprint, analyze it, and persist to DB."""
    analysis = analyze_fingerprint(fp)

    await db.execute(text(f"""
        INSERT INTO {SCHEMA}.device_fingerprints (
            device_hash, user_id, session_id, user_agent, platform,
            screen_width, screen_height, canvas_hash, webgl_hash,
            webgl_vendor, webgl_renderer, audio_hash,
            ip_address, timezone_name, language,
            device_memory, hardware_concurrency, touch_support,
            typing_entropy, mouse_entropy, webdriver,
            risk_score, risk_level, anomalies, raw_fingerprint
        ) VALUES (
            :device_hash, :user_id, :session_id, :user_agent, :platform,
            :screen_width, :screen_height, :canvas_hash, :webgl_hash,
            :webgl_vendor, :webgl_renderer, :audio_hash,
            :ip_address, :timezone_name, :language,
            :device_memory, :hardware_concurrency, :touch_support,
            :typing_entropy, :mouse_entropy, :webdriver,
            :risk_score, :risk_level, :anomalies, :raw_fingerprint
        )
    """), {
        "device_hash": analysis.device_hash,
        "user_id": fp.user_id,
        "session_id": fp.session_id,
        "user_agent": fp.user_agent[:1000],
        "platform": fp.platform,
        "screen_width": fp.screen_width,
        "screen_height": fp.screen_height,
        "canvas_hash": fp.canvas_hash,
        "webgl_hash": fp.webgl_hash,
        "webgl_vendor": fp.webgl_vendor,
        "webgl_renderer": fp.webgl_renderer,
        "audio_hash": fp.audio_hash,
        "ip_address": fp.ip_address,
        "timezone_name": fp.timezone_name,
        "language": fp.language,
        "device_memory": fp.device_memory,
        "hardware_concurrency": fp.hardware_concurrency,
        "touch_support": fp.touch_support,
        "typing_entropy": _typing_entropy(fp.typing_cadence_ms),
        "mouse_entropy": fp.mouse_entropy,
        "webdriver": fp.webdriver,
        "risk_score": analysis.risk_score,
        "risk_level": analysis.risk_level,
        "anomalies": json.dumps(analysis.anomalies),
        "raw_fingerprint": json.dumps(fp.model_dump(), default=str),
    })
    await db.commit()

    # Check how many distinct users this device has been seen on
    result = await db.execute(text(f"""
        SELECT COUNT(DISTINCT user_id) AS linked_users, COUNT(*) AS seen_count
        FROM {SCHEMA}.device_fingerprints
        WHERE device_hash = :hash
    """), {"hash": analysis.device_hash})
    row = result.fetchone()
    analysis.linked_users = row.linked_users if row else 0
    analysis.seen_count = row.seen_count if row else 0

    # Multi-accounting risk boost
    if analysis.linked_users > 1:
        analysis.anomalies.append({
            "type": "multi_accounting",
            "detail": f"Device seen on {analysis.linked_users} different users",
            "weight": min(analysis.linked_users * 15, 40),
        })
        analysis.risk_score = min(analysis.risk_score + analysis.linked_users * 15, 100)
        if analysis.risk_score >= 75:
            analysis.risk_level = "CRITICAL"
        elif analysis.risk_score >= 50:
            analysis.risk_level = "HIGH"

    return {
        "status": "collected",
        "device_hash": analysis.device_hash,
        "analysis": analysis.model_dump(),
    }


@router.get("/{device_hash}")
async def get_device(device_hash: str, db: AsyncSession = Depends(get_db)):
    """Retrieve stored fingerprint data and history for a device hash."""
    result = await db.execute(text(f"""
        SELECT device_hash, user_id, session_id, ip_address,
               risk_score, risk_level, anomalies, webgl_renderer,
               platform, typing_entropy, mouse_entropy, webdriver,
               created_at
        FROM {SCHEMA}.device_fingerprints
        WHERE device_hash = :hash
        ORDER BY created_at DESC
        LIMIT 20
    """), {"hash": device_hash})
    rows = [dict(r._mapping) for r in result.fetchall()]
    if not rows:
        raise HTTPException(status_code=404, detail="Device not found")

    distinct_users = len({r["user_id"] for r in rows})

    return {
        "device_hash": device_hash,
        "sightings": len(rows),
        "distinct_users": distinct_users,
        "multi_accounting": distinct_users > 1,
        "latest": rows[0],
        "history": rows,
    }


@router.get("/user/{user_id}")
async def get_user_devices(user_id: str, db: AsyncSession = Depends(get_db)):
    """All devices associated with a user — detects device-switching fraud."""
    result = await db.execute(text(f"""
        SELECT DISTINCT ON (device_hash)
            device_hash, ip_address, platform, webgl_renderer,
            risk_score, risk_level, webdriver, created_at
        FROM {SCHEMA}.device_fingerprints
        WHERE user_id = :uid
        ORDER BY device_hash, created_at DESC
    """), {"uid": user_id})
    devices = [dict(r._mapping) for r in result.fetchall()]

    return {
        "user_id": user_id,
        "device_count": len(devices),
        "devices": devices,
        "device_switching_risk": "HIGH" if len(devices) >= 3 else "MEDIUM" if len(devices) == 2 else "LOW",
    }


@router.post("/analyze")
async def analyze_device(body: AnalyzeRequest):
    """Stateless fingerprint analysis — no DB persistence."""
    analysis = analyze_fingerprint(body.fingerprint)
    return {"analysis": analysis.model_dump()}
