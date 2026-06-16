"""
Detect when tracking.my UI changes break Selenium parsing and alert the operator.

Configure via environment variables:

  PARSER_ALERT_WEBHOOK          POST JSON alert here (Slack, Discord, n8n, etc.)
  PARSER_ALERT_FILE             Append alerts to this log (default: parser_alerts.log)
  PARSER_ALERT_MACOS=1          Show a macOS desktop notification
  PARSER_ALERT_COOLDOWN_SECONDS Seconds before repeating the same alert (default: 3600)
  PARSER_ALERT_STATE_FILE       Cooldown state file (default: .parser_alert_state.json)
  PARSER_CANARY_TRACKING_NUMBER Known-good number for GET /api/health/parser
  PARSER_CANARY_COURIER         Optional courier slug for the canary lookup
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

_alert_lock = threading.Lock()

ISSUE_MESSAGES = {
    "selenium_error": "Selenium failed — check Chrome/chromedriver or site blocking.",
    "stuck_on_homepage": "Still on homepage (courier modal?) — Track/submit flow may have changed.",
    "results_page_not_reached": "Never reached a tracking results page.",
    "status_not_parsed": "Results page loaded but status could not be parsed — UI selectors likely changed.",
    "timeline_not_parsed": "Shipment timeline visible in page text but parser returned nothing.",
    "courier_not_detected": "On results URL but courier name/slug was not detected.",
    "page_not_found": "tracking.my returned a 404 page.",
    "canary_misconfigured": "Canary health check is not configured.",
}


def _health_message(issues: list[str]) -> str | None:
    if not issues:
        return None
    parts = [ISSUE_MESSAGES.get(code, code) for code in issues]
    return "Parser may need updates: " + " | ".join(parts)


def diagnose_parse_health(result: Any, payload: dict) -> dict:
    """Return parser health diagnostics for a scrape result."""
    issues: list[str] = []
    raw = result.raw_text or ""
    page_url = result.page_url or ""
    tracking_number = result.tracking_number
    shipment = payload.get("shipment") or {}

    if result.error:
        issues.append("selenium_error")

    if "Oh no! Page not found" in raw or "404 Not Found" in raw:
        issues.append("page_not_found")

    on_results_url = tracking_number in page_url
    has_timeline = "Shipment progress" in raw
    has_known_miss = "No result found" in raw or "Incorrect tracking number" in raw
    on_results_page = on_results_url or has_timeline or has_known_miss

    if "Select a courier" in raw and not on_results_page:
        issues.append("stuck_on_homepage")

    if "All-in-one Package Tracking" in raw and not on_results_page:
        issues.append("results_page_not_reached")

    if on_results_page and not has_known_miss:
        if has_timeline and not result.journey and not result.last_update:
            issues.append("timeline_not_parsed")
        if not result.status and not shipment.get("summary"):
            issues.append("status_not_parsed")
        if on_results_url and not result.courier_detected and not result.courier_slug:
            issues.append("courier_not_detected")

    healthy = not issues
    needs_fix = bool(issues)

    return {
        "healthy": healthy,
        "needs_fix": needs_fix,
        "issues": issues,
        "message": _health_message(issues),
    }


def _state_file() -> str:
    return os.environ.get("PARSER_ALERT_STATE_FILE", ".parser_alert_state.json")


def _load_alert_state() -> dict:
    path = _state_file()
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict) -> None:
    path = _state_file()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def _is_in_cooldown(alert_key: str) -> bool:
    cooldown = int(os.environ.get("PARSER_ALERT_COOLDOWN_SECONDS", "3600"))
    if cooldown <= 0:
        return False
    state = _load_alert_state()
    last_sent = state.get(alert_key)
    if not last_sent:
        return False
    return (time.time() - last_sent) < cooldown


def _mark_alert_sent(alert_key: str) -> None:
    state = _load_alert_state()
    state[alert_key] = time.time()
    _save_alert_state(state)


def _append_alert_file(body: dict) -> bool:
    path = os.environ.get("PARSER_ALERT_FILE", "parser_alerts.log")
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _post_webhook(body: dict) -> bool:
    webhook = os.environ.get("PARSER_ALERT_WEBHOOK", "").strip()
    if not webhook:
        return False
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _macos_notify(title: str, message: str) -> bool:
    if os.environ.get("PARSER_ALERT_MACOS", "").strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return False
    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}" sound name "Basso"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def maybe_send_parser_alert(
    health: dict,
    result: Any,
    payload: dict,
    *,
    force: bool = False,
) -> dict:
    """Send alert when parser health is bad. Rate-limited by issue signature."""
    if not health.get("needs_fix"):
        return {"sent": False, "reason": "healthy"}

    alert_key = ",".join(sorted(health.get("issues") or []))
    if not force and _is_in_cooldown(alert_key):
        return {"sent": False, "reason": "cooldown", "cooldown_key": alert_key}

    body = {
        "event": "parser_needs_fix",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "issues": health.get("issues"),
        "message": health.get("message"),
        "tracking_number": result.tracking_number,
        "courier_input": result.courier_input or None,
        "source_url": result.page_url or None,
        "selenium_error": result.error or None,
        "shipment_found": (payload.get("shipment") or {}).get("found"),
        "action": "Review app.py Selenium selectors and parser_health.py rules.",
    }

    channels: list[str] = []
    with _alert_lock:
        if _append_alert_file(body):
            channels.append("file")
        if _post_webhook(body):
            channels.append("webhook")
        if _macos_notify("tracking.my parser", health.get("message") or alert_key):
            channels.append("macos")
        if channels or force:
            _mark_alert_sent(alert_key)

    if not channels:
        return {
            "sent": False,
            "reason": "no_channels_configured",
            "hint": (
                "Set PARSER_ALERT_WEBHOOK, PARSER_ALERT_FILE (default on), "
                "or PARSER_ALERT_MACOS=1"
            ),
        }

    return {"sent": True, "channels": channels, "cooldown_key": alert_key}


def attach_parser_health(
    payload: dict,
    result: Any,
    *,
    send_alert: bool = True,
    force_alert: bool = False,
) -> dict:
    health = diagnose_parse_health(result, payload)
    if send_alert:
        health["alert"] = maybe_send_parser_alert(
            health, result, payload, force=force_alert
        )
    else:
        health["alert"] = {"sent": False, "reason": "skipped"}
    payload["parser_health"] = health
    if health.get("needs_fix"):
        payload["ok"] = False
    return payload
