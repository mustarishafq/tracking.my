"""
app.py - Local web app + JSON API for checking tracking.my shipment status.

Runs a small Flask server with:
  - A simple search page:        http://localhost:5000/
  - A single-lookup JSON API:    GET  /api/track?tracking_number=XXX&courier=YYY
  - A batch JSON API:            POST /api/track/batch
                                  body: {"items": [{"tracking_number": "...", "courier": "..."}]}
  - Parser health check:         GET  /api/health/parser

Parser break detection (when tracking.my UI changes):
  Set PARSER_ALERT_MACOS=1 and/or PARSER_ALERT_WEBHOOK=https://...
  Optional canary: PARSER_CANARY_TRACKING_NUMBER=680081624752013
  See parser_health.py for all alert env vars.

Under the hood it drives a headless Chrome via Selenium against
https://www.tracking.my (no tracking.my account / API key needed).

CAVEAT (same as before): tracking.my's results markup wasn't inspected
live in this session, so status/last_update extraction is heuristic
(keyword + date-pattern matching over the rendered page text). Every
response includes `raw_text` (the full page text Selenium saw) so you can
verify, and `error` if something broke. If status keeps coming back
empty, send me a `raw_text` sample and I'll tighten the parser.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000 in your browser, or call the API directly.
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from parser_health import attach_parser_health
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

BASE_URL = "https://www.tracking.my/"
HEADLESS = True

# Common courier aliases -> tracking.my URL slug (see /couriers).
COURIER_SLUGS = {
    "pos": "poslaju",
    "poslaju": "poslaju",
    "pos malaysia": "poslaju",
    "pos-malaysia": "poslaju",
    "posmalaysia": "poslaju",
    "jt": "jt",
    "j&t": "jt",
    "jnt": "jt",
    "j&t express": "jt",
    "jnt express": "jt",
    "j and t": "jt",
    "shopee": "spx",
    "spx": "spx",
    "spx express": "spx",
    "shopee express": "spx",
    "dhl": "dhl",
    "dhl ecommerce": "dhl",
    "gdex": "gdex",
    "gd express": "gdex",
    "ninjavan": "ninjavan",
    "ninja van": "ninjavan",
    "citylink": "citylink",
    "city-link": "citylink",
    "citylink express": "citylink",
    "flash": "flash",
    "flash express": "flash",
    "best": "best",
    "best express": "best",
    "abx": "abx",
    "abx express": "abx",
    "skynet": "skynet",
    "skynet express": "skynet",
    "lex": "lex",
    "pgeon": "pgeon",
    "pgeon delivery": "pgeon",
    "posstore": "posstore",
}

STATUS_KEYWORDS = [
    "Delivered", "Dihantar", "Telah dihantar",
    "Out for delivery", "Sedang diantar", "Dalam penghantaran",
    "In transit", "Dalam perjalanan", "Dalam transit",
    "Picked up", "Diambil",
    "Arrived at", "Tiba di",
    "Departed from", "Berlepas dari",
    "Pending", "Belum diambil",
    "Order created", "Info received", "Maklumat diterima",
    "Returned", "Dipulangkan",
    "Failed delivery", "Gagal dihantar",
    "Exception", "Tidak dijumpai", "Not found",
]

# Selenium WebDriver instances aren't safe to share across concurrent
# requests. Spinning a fresh headless Chrome per lookup and serializing
# requests with a lock is the simplest safe setup for a personal/local
# tool. Each lookup takes a few seconds.
_lock = threading.Lock()


@dataclass
class TrackResult:
    tracking_number: str
    courier_input: str = ""
    courier_detected: str = ""
    courier_slug: str = ""
    page_url: str = ""
    status: str = ""
    last_update: str = ""
    journey: list = field(default_factory=list)
    raw_text: str = ""
    error: str = ""


def _path_without_stale_chromedriver() -> str:
    """Drop PATH entries that contain a chromedriver binary.

    An outdated chromedriver on PATH (common on macOS after Chrome auto-updates)
    overrides Selenium Manager and causes SessionNotCreatedException.
    """
    parts = os.environ.get("PATH", "").split(os.pathsep)
    safe = [
        p
        for p in parts
        if p and not os.path.isfile(os.path.join(p, "chromedriver"))
    ]
    return os.pathsep.join(safe)


def build_driver():
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1366,2200")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    old_path = os.environ.get("PATH")
    try:
        os.environ["PATH"] = _path_without_stale_chromedriver()
        return webdriver.Chrome(service=Service(), options=opts)
    finally:
        if old_path is not None:
            os.environ["PATH"] = old_path
        else:
            os.environ.pop("PATH", None)


def normalize_courier_slug(courier: str) -> str:
    key = courier.strip().lower()
    if key in COURIER_SLUGS:
        return COURIER_SLUGS[key]
    slug = re.sub(r"[^a-z0-9]+", "", key)
    return COURIER_SLUGS.get(slug, slug)


def find_tracking_input(driver):
    try:
        el = driver.find_element(By.ID, "formTrackingInputHome")
        if el.is_displayed():
            return el
    except NoSuchElementException:
        pass
    candidates = [
        "//input[contains(translate(@placeholder,'TRACKINGNUMBER','trackingnumber'),'tracking')]",
        "//input[contains(translate(@aria-label,'TRACKINGNUMBER','trackingnumber'),'tracking')]",
        "//input[@type='text']",
    ]
    for xpath in candidates:
        els = driver.find_elements(By.XPATH, xpath)
        for el in els:
            if el.is_displayed():
                return el
    raise NoSuchElementException("Could not locate tracking number input box")


def navigate_to_tracking(driver, wait, tracking_number: str, courier: str = ""):
    """Open the tracking results page for a parcel."""
    slug = normalize_courier_slug(courier) if courier else ""
    if slug:
        driver.get(f"{BASE_URL}{slug}/{tracking_number}")
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(3)
        return

    driver.get(BASE_URL)
    box = wait.until(EC.element_to_be_clickable((By.ID, "formTrackingInputHome")))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", box)
    box.click()
    box.clear()
    box.send_keys(tracking_number)
    time.sleep(1.5)  # allow auto-detect to fill the courier field
    box.send_keys(Keys.ENTER)

    def _results_loaded(drv):
        body = drv.find_element(By.TAG_NAME, "body").text
        return (
            tracking_number in drv.current_url
            or "Shipment progress" in body
            or "No result found" in body
        )

    wait.until(_results_loaded)
    time.sleep(1)


TIMELINE_STOP = frozenset({
    "Tracking number",
    "Courier",
    "Track",
    "Scan to download",
    "Articles",
    "Contact Us",
    "Careers",
    "Terms",
    "Privacy",
})
TIMELINE_SKIP_PREFIXES = (
    "Show all",
    "Get real-time",
    "Send your parcel",
    "Choose from",
    "Full time",
    "Grow your career",
    "ADVERTISEMENT",
    "View ad",
)
DATE_LINE = re.compile(
    r"^\d{1,2}\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.I,
)
TIME_LINE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$", re.I)


def expand_shipment_timeline(driver):
    """Click 'Show all updates' so the full journey is in the page text."""
    try:
        for el in driver.find_elements(
            By.XPATH, "//*[contains(text(),'Show all updates')]"
        ):
            if el.is_displayed():
                driver.execute_script("arguments[0].click();", el)
                time.sleep(1.5)
                return
    except NoSuchElementException:
        pass


def _skip_timeline_line(line: str) -> bool:
    return line.startswith(TIMELINE_SKIP_PREFIXES) or line.isdigit()


def extract_journey(body_text: str) -> list[dict]:
    """Parse all shipment timeline events from tracking.my results text."""
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    if "Shipment progress" not in lines:
        return []

    events: list[dict] = []
    i = lines.index("Shipment progress") + 1
    while i < len(lines):
        line = lines[i]
        if line in TIMELINE_STOP:
            break
        if _skip_timeline_line(line):
            i += 1
            continue
        if (
            DATE_LINE.match(line)
            and i + 1 < len(lines)
            and TIME_LINE.match(lines[i + 1])
        ):
            date = line
            time = lines[i + 1]
            description = lines[i + 2] if i + 2 < len(lines) else ""
            location = ""
            i += 3
            if i < len(lines):
                nxt = lines[i]
                if (
                    not DATE_LINE.match(nxt)
                    and not TIME_LINE.match(nxt)
                    and not _skip_timeline_line(nxt)
                    and nxt not in TIMELINE_STOP
                ):
                    location = nxt
                    i += 1
            events.append(
                {
                    "date": date,
                    "time": time,
                    "description": description,
                    "location": location or None,
                }
            )
            continue
        i += 1
    return events


def journey_event_to_pipe(event: dict) -> str:
    parts = [
        event.get("date") or "",
        event.get("time") or "",
        event.get("description") or "",
        event.get("location") or "",
    ]
    return " | ".join(part for part in parts if part)


def year_from_status(summary: str) -> int | None:
    match = re.search(r"\d{4}", summary or "")
    return int(match.group()) if match else None


def maybe_select_courier(driver, courier: str):
    if not courier:
        return
    slug = normalize_courier_slug(courier)
    try:
        courier_input = driver.find_element(By.ID, "formCourierInputHome")
        courier_input.click()
        courier_input.clear()
        courier_input.send_keys(slug)
        time.sleep(1)
        suggestions = driver.find_elements(
            By.XPATH, f"//li[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{slug}')]"
        )
        if suggestions:
            suggestions[0].click()
    except NoSuchElementException:
        pass


def extract_status_and_history(driver, body_text: str):
    status_line = ""
    try:
        h2 = driver.find_element(By.CSS_SELECTOR, "h2")
        status_line = h2.text.strip().splitlines()[0]
    except NoSuchElementException:
        pass

    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    if not status_line:
        for el in driver.find_elements(By.CSS_SELECTOR, "[class*='status']"):
            text = el.text.strip().splitlines()[0] if el.text.strip() else ""
            if text and len(text) > 3:
                status_line = text
                break

    if not status_line:
        for line in lines:
            for kw in STATUS_KEYWORDS:
                if kw.lower() in line.lower():
                    status_line = line
                    break
            if status_line:
                break

    journey = extract_journey(body_text)
    return status_line, journey


def detect_courier_from_page(body_text: str, tracking_number: str, page_url: str) -> tuple[str, str]:
    slug = ""
    slug_match = re.search(rf"/([^/?#]+)/{re.escape(tracking_number)}", page_url)
    if slug_match:
        slug = slug_match.group(1)

    detected = ""
    marker = f"#{tracking_number}"
    if marker in body_text:
        before = body_text.split(marker, 1)[0].splitlines()
        skip = {"Send Parcel", "Products", "English", "Contact"}
        for line in reversed(before):
            line = line.strip()
            if line and line not in skip:
                detected = line
                break

    return detected, slug


def format_delivery_datetime(
    delivery_date_str: str | None,
    event_date: str | None = None,
    event_time: str | None = None,
    reference_year: int | None = None,
) -> str | None:
    """Normalize delivery date/time to yyyy-mm-dd HH:mm:ss."""
    dt = None

    if delivery_date_str:
        cleaned = re.sub(r"\s*\([^)]*\)", "", delivery_date_str).strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %b %Y", "%d %B %Y"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                break
            except ValueError:
                continue

    if dt is None and event_date:
        year_match = re.search(r"\d{4}", delivery_date_str or "")
        year = (
            int(year_match.group())
            if year_match
            else reference_year or datetime.now().year
        )
        for fmt in ("%d %b", "%d %B"):
            try:
                dt = datetime.strptime(f"{event_date} {year}", f"{fmt} %Y")
                break
            except ValueError:
                continue

    if dt and event_time:
        for fmt in ("%I:%M %p", "%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(event_time.strip(), fmt)
                dt = dt.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute,
                    second=parsed_time.second,
                )
                break
            except ValueError:
                continue
    elif dt:
        dt = dt.replace(hour=0, minute=0, second=0)

    if dt:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return None


def parse_shipment(summary: str, raw_text: str) -> dict:
    found = "No result found" not in raw_text and "Incorrect tracking number" not in raw_text

    status_label = None
    delivery_date = None
    transit_note = None

    if summary:
        parts = [part.strip() for part in summary.split("·")]
        if parts:
            status_label = parts[0] or None
        for part in parts[1:]:
            lower = part.lower()
            if lower.startswith("delivery date:"):
                delivery_date = part.split(":", 1)[1].strip() or None
            elif "transit" in lower:
                transit_note = part.strip() or None

    if not found:
        status_label = status_label or "Not found"

    return {
        "found": found and bool(summary),
        "status": status_label,
        "summary": summary or None,
        "delivery_date": delivery_date,
        "transit_note": transit_note,
    }


def enrich_journey(journey: list[dict], summary: str, shipment: dict) -> list[dict]:
    """Add normalized datetime to each journey step."""
    raw_delivery = shipment.get("delivery_date")
    if raw_delivery and re.match(r"^\d{4}-\d{2}-\d{2}", str(raw_delivery)):
        reference_year = int(str(raw_delivery)[:4])
    else:
        reference_year = year_from_status(summary)

    enriched = []
    for event in journey:
        item = dict(event)
        dt = format_delivery_datetime(
            None,
            item.get("date"),
            item.get("time"),
            reference_year=reference_year,
        )
        if dt:
            item["datetime"] = dt
        enriched.append(item)
    return enriched


def parse_latest_event(journey: list[dict]) -> dict | None:
    if not journey:
        return None
    return dict(journey[0])


def parse_error(error: str) -> dict | None:
    if not error:
        return None
    err_type, _, message = error.partition(": ")
    return {
        "type": err_type or "Error",
        "message": message or error,
    }


def result_to_payload(result: TrackResult) -> dict:
    shipment = parse_shipment(result.status, result.raw_text)
    journey = enrich_journey(result.journey, result.status, shipment)
    latest_event = parse_latest_event(journey)
    error = parse_error(result.error)

    formatted_delivery = format_delivery_datetime(
        shipment.get("delivery_date"),
        (latest_event or {}).get("date"),
        (latest_event or {}).get("time"),
        reference_year=year_from_status(result.status),
    )
    if formatted_delivery:
        shipment["delivery_date"] = formatted_delivery

    return {
        "ok": error is None and (shipment["found"] or "No result found" in result.raw_text),
        "tracking_number": result.tracking_number,
        "courier": {
            "input": result.courier_input or None,
            "detected": result.courier_detected or None,
            "slug": result.courier_slug or None,
        },
        "shipment": shipment,
        "latest_event": latest_event,
        "journey": journey,
        "journey_count": len(journey),
        "source_url": result.page_url or None,
        "error": error,
        "debug": {
            "raw_text": result.raw_text or None,
        },
    }


def build_api_response(
    result: TrackResult,
    *,
    send_alert: bool = True,
    force_alert: bool = False,
) -> dict:
    payload = result_to_payload(result)
    return attach_parser_health(
        payload,
        result,
        send_alert=send_alert,
        force_alert=force_alert,
    )


def track_one(tracking_number: str, courier: str = "") -> TrackResult:
    result = TrackResult(tracking_number=tracking_number, courier_input=courier)
    with _lock:
        driver = None
        try:
            driver = build_driver()
            wait = WebDriverWait(driver, 20)
            navigate_to_tracking(driver, wait, tracking_number, courier)
            expand_shipment_timeline(driver)

            body_text = driver.find_element(By.TAG_NAME, "body").text
            result.raw_text = body_text
            result.page_url = driver.current_url
            detected, slug = detect_courier_from_page(
                body_text, tracking_number, result.page_url
            )
            result.courier_detected = detected
            result.courier_slug = slug or (
                normalize_courier_slug(courier) if courier else ""
            )
            status_line, journey = extract_status_and_history(driver, body_text)
            result.status = status_line
            result.journey = journey
            result.last_update = journey_event_to_pipe(journey[0]) if journey else ""
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
        finally:
            if driver is not None:
                driver.quit()
    return result


app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/track")
def api_track():
    tn = request.args.get("tracking_number", "").strip()
    courier = request.args.get("courier", "").strip()
    if not tn:
        return jsonify({"ok": False, "error": {"type": "ValidationError", "message": "tracking_number query param is required"}}), 400
    result = track_one(tn, courier)
    payload = build_api_response(result)
    status = 200 if payload.get("ok") else 503
    return jsonify(payload), status


@app.route("/api/health/parser")
def api_health_parser():
    """Run a canary lookup to verify Selenium + parser still work."""
    dry_run = request.args.get("dry_run", "").lower() in ("1", "true", "yes")
    force_alert = request.args.get("force_alert", "").lower() in ("1", "true", "yes")

    tn = request.args.get("tracking_number", "").strip()
    courier = request.args.get("courier", "").strip()
    if not tn:
        tn = os.environ.get("PARSER_CANARY_TRACKING_NUMBER", "").strip()
        courier = courier or os.environ.get("PARSER_CANARY_COURIER", "").strip()

    if not tn:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "type": "Misconfigured",
                        "message": (
                            "Set PARSER_CANARY_TRACKING_NUMBER or pass "
                            "?tracking_number=YOUR_KNOWN_GOOD_NUMBER"
                        ),
                    },
                    "parser_health": {
                        "healthy": False,
                        "needs_fix": True,
                        "issues": ["canary_misconfigured"],
                        "message": "Canary health check is not configured.",
                        "alert": {"sent": False, "reason": "skipped"},
                    },
                }
            ),
            503,
        )

    result = track_one(tn, courier)
    payload = build_api_response(
        result,
        send_alert=not dry_run,
        force_alert=force_alert,
    )
    status = 200 if payload.get("parser_health", {}).get("healthy") else 503
    return jsonify(payload), status


@app.route("/api/track/batch", methods=["POST"])
def api_track_batch():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": {
                        "type": "ValidationError",
                        "message": (
                            "body must be {'items': [{'tracking_number': '...', "
                            "'courier': '...'}, ...]}"
                        ),
                    },
                }
            ),
            400,
        )
    results = []
    for item in items:
        tn = (item.get("tracking_number") or "").strip()
        courier = (item.get("courier") or "").strip()
        if not tn:
            continue
        results.append(build_api_response(track_one(tn, courier)))
    all_ok = all(item.get("ok") for item in results)
    return jsonify({"ok": all_ok, "count": len(results), "results": results})


if __name__ == "__main__":
    # Port 5000 is often taken on macOS by AirPlay Receiver, so default to
    # 5050 instead. Override with: PORT=5000 python3 app.py
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
