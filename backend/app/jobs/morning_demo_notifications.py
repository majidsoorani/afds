"""Morning demo runner.

Runs 15 suites with 3 tests each (45 total) and sends one Teams webhook
message per suite.

Run locally:
    cd backend
    DEMO_BASE_URL='http://localhost:8000' \
    DEMO_TEAMS_WEBHOOK_URL='<your-webhook-url>' \
    /opt/homebrew/anaconda3/bin/python3.12 -m app.jobs.morning_demo_notifications
"""

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = os.getenv("DEMO_BASE_URL", "http://afds-backend:8000").rstrip("/")
WEBHOOK_URL = os.getenv("DEMO_TEAMS_WEBHOOK_URL", "").strip()
REQUEST_TIMEOUT_SECONDS = float(os.getenv("DEMO_REQUEST_TIMEOUT_SECONDS", "20"))
JOB_TIMEOUT_SECONDS = int(os.getenv("DEMO_JOB_TIMEOUT_SECONDS", "1800"))


@dataclass
class TestResult:
    suite: str
    name: str
    passed: bool
    details: str
    duration_ms: int


@dataclass(frozen=True)
class HttpTestSpec:
    suite: str
    name: str
    method: str
    path: str
    payload: dict | None = None
    expected_statuses: tuple[int, ...] = (200,)


@dataclass(frozen=True)
class JobTestSpec:
    suite: str
    name: str
    module: str


def _json_preview(payload: object, max_len: int = 200) -> str:
    txt = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    if len(txt) <= max_len:
        return txt
    return f"{txt[:max_len]}..."


def _compact_text(value: str, max_len: int = 140) -> str:
    one_line = " ".join((value or "").split())
    if len(one_line) <= max_len:
        return one_line
    return f"{one_line[:max_len]}..."


def _summarize_details(result: TestResult) -> str:
    details = result.details or ""

    if "status=" in details:
        status_match = None
        body_match = None
        try:
            import re

            status_match = re.search(r"status=(\d+)", details)
            body_match = re.search(r"body=(.*)", details)
        except Exception:
            status_match = None
            body_match = None

        status_txt = status_match.group(1) if status_match else "?"
        if result.passed:
            return f"HTTP {status_txt}"
        body_txt = _compact_text(body_match.group(1) if body_match else details)
        return f"HTTP {status_txt} | {body_txt}"

    if "exit=" in details:
        if result.passed:
            return "completed successfully"
        return _compact_text(details)

    return _compact_text(details)


def _device_payload(user_id: str, webdriver: bool, entropy: float) -> dict:
    return {
        "fingerprint": {
            "user_id": user_id,
            "session_id": f"{user_id}-session",
            "user_agent": "Mozilla/5.0 DemoAgent",
            "platform": "Linux",
            "webgl_renderer": "SwiftShader",
            "webgl_vendor": "Google Inc.",
            "canvas_hash": f"canvas-{user_id}",
            "webgl_hash": f"webgl-{user_id}",
            "webdriver": webdriver,
            "mouse_movements": 18,
            "mouse_entropy": entropy,
            "typing_cadence_ms": [120, 130, 110, 115, 140],
        }
    }


HTTP_TESTS: list[HttpTestSpec] = [
    HttpTestSpec("01 Core", "Health", "GET", "/health"),
    HttpTestSpec("01 Core", "OpenAPI", "GET", "/openapi.json"),
    HttpTestSpec("01 Core", "Metrics", "GET", "/metrics"),

    HttpTestSpec("02 Rule Chat", "Suggestions", "GET", "/api/v1/rule-chat/suggestions"),
    HttpTestSpec("02 Rule Chat", "Fields", "GET", "/api/v1/rule-chat/fields"),
    HttpTestSpec(
        "02 Rule Chat",
        "Parse",
        "POST",
        "/api/v1/rule-chat/parse",
        {"message": "Block transactions over 50000"},
    ),

    HttpTestSpec("03 Rules", "List Active False", "GET", "/api/v1/rules/?active_only=false"),
    HttpTestSpec("03 Rules", "List Active True", "GET", "/api/v1/rules/?active_only=true"),
    HttpTestSpec("03 Rules", "List Limit", "GET", "/api/v1/rules/?active_only=false&limit=10"),

    HttpTestSpec(
        "04 Sanctions",
        "Screen Putin",
        "POST",
        "/api/v1/sanctions/screen",
        {"name": "Vladimir Putin", "threshold": 0.6},
    ),
    HttpTestSpec(
        "04 Sanctions",
        "Screen Khamenei",
        "POST",
        "/api/v1/sanctions/screen",
        {"name": "Ali Khamenei", "threshold": 0.6},
    ),
    HttpTestSpec(
        "04 Sanctions",
        "Screen Random",
        "POST",
        "/api/v1/sanctions/screen",
        {"name": "John Doe", "threshold": 0.6},
    ),

    HttpTestSpec(
        "05 Enrichment",
        "Email",
        "POST",
        "/api/v1/enrichment/email",
        {"email": "daily-demo@example.com", "entity_id": "daily-demo-user"},
    ),
    HttpTestSpec(
        "05 Enrichment",
        "IP",
        "POST",
        "/api/v1/enrichment/ip",
        {"ip_address": "34.23.55.100", "entity_id": "daily-demo-user"},
    ),
    HttpTestSpec(
        "05 Enrichment",
        "Phone",
        "POST",
        "/api/v1/enrichment/phone",
        {"phone": "+447911123456", "entity_id": "daily-demo-user"},
    ),

    HttpTestSpec("06 Device", "Analyze Standard", "POST", "/api/v1/device/analyze", _device_payload("daily-demo-a", False, 0.63)),
    HttpTestSpec("06 Device", "Analyze Webdriver", "POST", "/api/v1/device/analyze", _device_payload("daily-demo-b", True, 0.12)),
    HttpTestSpec("06 Device", "Analyze Low Entropy", "POST", "/api/v1/device/analyze", _device_payload("daily-demo-c", False, 0.08)),

    HttpTestSpec("07 Network", "Communities", "GET", "/api/v1/network/communities"),
    HttpTestSpec("07 Network", "Graph User", "GET", "/api/v1/network/graph/user-001"),
    HttpTestSpec("07 Network", "Fund Flow User", "GET", "/api/v1/network/fund-flow/user-001"),

    HttpTestSpec("09 Realtime", "State", "GET", "/api/v1/realtime/state"),
    HttpTestSpec("09 Realtime", "Simulate", "GET", "/api/v1/realtime/simulate"),
    HttpTestSpec("09 Realtime", "Reset", "POST", "/api/v1/realtime/reset", {}),

    HttpTestSpec("10 Reporting", "Stats", "GET", "/api/v1/reporting/stats"),
    HttpTestSpec("10 Reporting", "SAR List", "GET", "/api/v1/reporting/sar"),
    HttpTestSpec("10 Reporting", "SAR Filtered", "GET", "/api/v1/reporting/sar?status=FILED"),

    HttpTestSpec("11 Alerts", "Open", "GET", "/api/v1/alerts/?status=OPEN"),
    HttpTestSpec("11 Alerts", "Escalated", "GET", "/api/v1/alerts/?status=ESCALATED"),
    HttpTestSpec("11 Alerts", "All", "GET", "/api/v1/alerts/"),

    HttpTestSpec("12 Transactions", "List", "GET", "/api/v1/transactions/"),
    HttpTestSpec("12 Transactions", "List Limit", "GET", "/api/v1/transactions/?limit=5"),
    HttpTestSpec("12 Transactions", "List Sender", "GET", "/api/v1/transactions/?sender_id=user-001"),

    HttpTestSpec("13 Debug", "Entity user-001", "GET", "/api/v1/debug/entity/user-001"),
    HttpTestSpec("13 Debug", "Entity compare-user-1", "GET", "/api/v1/debug/entity/compare-user-1"),
    HttpTestSpec("13 Debug", "Transaction known", "GET", "/api/v1/debug/transaction/893df161-00b3-44fb-a6ce-f38622565bc4"),

]


JOB_TESTS: list[JobTestSpec] = [
    JobTestSpec("15 Jobs", "Generate Daily Report", "app.jobs.generate_daily_report"),
    JobTestSpec("15 Jobs", "Escalate Stale Alerts", "app.jobs.escalate_stale_alerts"),
    JobTestSpec("15 Jobs", "Batch Risk Rescoring", "app.jobs.batch_risk_rescoring"),
]

# ── Per-suite display titles shown in the Teams card header ──────────
# Change any value here to rename that card's title in Teams.
SUITE_TITLES: dict[str, str] = {
    "01 Core":         "Core API",
    "02 Rule Chat":    "Rule Chat",
    "03 Rules":        "Rules Engine",
    "04 Sanctions":    "Sanctions Screening",
    "05 Enrichment":   "Enrichment",
    "06 Device":       "Device Intelligence",
    "07 Network":      "Network Analysis",
    "09 Realtime":     "Real-time Engine",
    "10 Reporting":    "Reporting & SAR",
    "11 Alerts":       "Alert Management",
    "12 Transactions": "Transactions",
    "13 Debug":        "Debugger",
    "14 third-party vendor":         "third-party vendor Comparison",
    "15 Jobs":         "Scheduled Jobs",
}

# ── Representative curl command per suite (first test in the suite) ─
_SUITE_CURL: dict[str, str] = {}
for _spec in HTTP_TESTS:
    if _spec.suite not in _SUITE_CURL:
        _url = f"{BASE_URL}{_spec.path}"
        if _spec.method == "GET":
            _SUITE_CURL[_spec.suite] = f"curl -s '{_url}'"
        else:
            import json as _json
            _payload = _json.dumps(_spec.payload or {}, separators=(",", ":"))
            _SUITE_CURL[_spec.suite] = (
                f"curl -s -X POST '{_url}' "
                f"-H 'Content-Type: application/json' "
                f"-d '{_payload}'"
            )
for _jspec in JOB_TESTS:
    if _jspec.suite not in _SUITE_CURL:
        _SUITE_CURL[_jspec.suite] = (
            f"cd backend && /opt/homebrew/anaconda3/bin/python3.12 -m {_jspec.module}"
        )


def _run_http_tests() -> dict[str, list[TestResult]]:
    grouped: dict[str, list[TestResult]] = {}
    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        for spec in HTTP_TESTS:
            started = time.time()
            url = f"{BASE_URL}{spec.path}"
            try:
                if spec.method == "GET":
                    res = client.get(url)
                else:
                    res = client.post(url, json=spec.payload)

                passed = res.status_code in spec.expected_statuses
                body_preview = _json_preview(res.json()) if "application/json" in res.headers.get("content-type", "") else res.text[:200]
                details = f"status={res.status_code} body={body_preview}"
            except Exception as exc:
                passed = False
                details = f"error={exc}"

            duration_ms = int((time.time() - started) * 1000)
            grouped.setdefault(spec.suite, []).append(
                TestResult(suite=spec.suite, name=spec.name, passed=passed, details=details, duration_ms=duration_ms)
            )

    return grouped


def _run_job_tests() -> dict[str, list[TestResult]]:
    grouped: dict[str, list[TestResult]] = {}
    for spec in JOB_TESTS:
        started = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", spec.module],
                capture_output=True,
                text=True,
                timeout=JOB_TIMEOUT_SECONDS,
            )
            passed = proc.returncode == 0
            tail = "\n".join((proc.stdout or "").splitlines()[-4:])
            err_tail = "\n".join((proc.stderr or "").splitlines()[-4:])
            details = f"exit={proc.returncode} out_tail={tail[:300]} err_tail={err_tail[:300]}"
        except subprocess.TimeoutExpired:
            passed = False
            details = f"timeout>{JOB_TIMEOUT_SECONDS}s"
        except Exception as exc:
            passed = False
            details = f"error={exc}"

        duration_ms = int((time.time() - started) * 1000)
        grouped.setdefault(spec.suite, []).append(
            TestResult(suite=spec.suite, name=spec.name, passed=passed, details=details, duration_ms=duration_ms)
        )

    return grouped


def _format_card(group_name: str, results: list[TestResult]) -> dict:
    passed_count = sum(1 for r in results if r.passed)
    failed_count = len(results) - passed_count
    all_pass = failed_count == 0

    theme_color = "2ECC71" if all_pass else "E74C3C"
    status_prefix = "✅" if all_pass else "❌"

    # Use SUITE_TITLES if defined, otherwise strip the numeric prefix
    label = SUITE_TITLES.get(group_name, group_name.split(" ", 1)[1] if " " in group_name else group_name)

    facts = []
    for idx, r in enumerate(results, start=1):
        icon = "✅" if r.passed else "❌"
        detail = _summarize_details(r)
        facts.append({
            "name": f"{icon} {idx}. {r.name}",
            "value": f"{r.duration_ms} ms — {detail}",
        })

    curl_cmd = _SUITE_CURL.get(group_name, f"curl -s '{BASE_URL}/health'")

    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": theme_color,
        "summary": f"{status_prefix} {label}",
        "title": f"{status_prefix} {label}",
        "sections": [
            {
                "activitySubtitle": f"Tests: ✅ {passed_count} passed  ❌ {failed_count} failed  🧪 {len(results)} total",
                "facts": facts,
                "markdown": False,
            },
            {
                "facts": [{"name": "🧪 Test curl", "value": curl_cmd}],
                "markdown": False,
            },
        ],
    }


def _send_webhook_message(card: dict) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DEMO_TEAMS_WEBHOOK_URL is not configured")

    with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        res = client.post(WEBHOOK_URL, json=card)
        res.raise_for_status()


def run() -> int:
    logger.info("Starting AFDS morning demo notifications job (15 messages / 45 tests)")

    grouped_results: dict[str, list[TestResult]] = {}
    grouped_results.update(_run_http_tests())
    grouped_results.update(_run_job_tests())

    expected_suites = 15
    if len(grouped_results) != expected_suites:
        logger.error(f"Expected {expected_suites} suites, got {len(grouped_results)}")
        return 1

    total_tests = 0
    all_results: list[TestResult] = []
    for suite_name in sorted(grouped_results.keys()):
        suite_results = grouped_results[suite_name]
        if len(suite_results) != 3:
            logger.error(f"Suite {suite_name} must have exactly 3 tests; got {len(suite_results)}")
            return 1
        total_tests += len(suite_results)
        all_results.extend(suite_results)
        _send_webhook_message(_format_card(suite_name, suite_results))

    if total_tests != 45:
        logger.error(f"Expected 45 tests, got {total_tests}")
        return 1

    has_failure = any(not r.passed for r in all_results)
    if has_failure:
        logger.warning("Morning demo finished with failures")
        return 1

    logger.info("Morning demo finished successfully (15 messages / 45 tests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
