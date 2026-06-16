"""
track_tracking_my.py

Selenium script to look up shipment/tracking status from https://www.tracking.my
using the public search widget on the homepage (no API key required).

Why Selenium and not the official API?
- tracking.my does offer an official "Tracking API" (webhook-based) via
  https://seller.tracking.my -> API Keys. That is the more robust, long-term
  option (no breakage when the site's HTML changes, real-time webhook push).
- This script is for cases where you just want to batch-check tracking
  numbers on demand without registering for API access.

IMPORTANT CAVEAT
----------------
The exact CSS classes used by tracking.my's results panel could not be
verified live while writing this script (browser inspection tool was
unavailable in this session). The selectors below use resilient,
text/placeholder-based XPath (less likely to break than guessing CSS class
names), and the script ALSO saves a full text dump + screenshot for every
tracking number into the `debug/` folder. If `status` or `history` come back
empty after your first real run, open one of those debug files, find the
real text, and tell me what's different so I can tighten the parser.

Usage
-----
1. pip install -r requirements.txt
2. Put tracking numbers in tracking_numbers.csv (one per row, see sample).
3. python track_tracking_my.py
4. Results land in results.csv ; raw debug dumps land in debug/

Requires Google Chrome installed locally. Uses Selenium's built-in
Selenium Manager (Selenium >= 4.6) to fetch the matching chromedriver
automatically -- no manual driver download needed.
"""

import csv
import os
import re
from dataclasses import dataclass

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app import (
    expand_shipment_timeline,
    extract_status_and_history,
    journey_event_to_pipe,
    navigate_to_tracking,
)

INPUT_CSV = "tracking_numbers.csv"
OUTPUT_CSV = "results.csv"
DEBUG_DIR = "debug"

HEADLESS = True  # set False to watch the browser while debugging


@dataclass
class TrackResult:
    tracking_number: str
    courier_input: str = ""
    courier_detected: str = ""
    status: str = ""
    last_update: str = ""
    raw_text: str = ""
    error: str = ""


def _path_without_stale_chromedriver() -> str:
    """Drop PATH entries that contain a chromedriver binary."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    safe = [
        p
        for p in parts
        if p and not os.path.isfile(os.path.join(p, "chromedriver"))
    ]
    return os.pathsep.join(safe)


def build_driver(headless: bool = HEADLESS) -> webdriver.Chrome:
    opts = Options()
    if headless:
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
        driver = webdriver.Chrome(service=Service(), options=opts)
    finally:
        if old_path is not None:
            os.environ["PATH"] = old_path
        else:
            os.environ.pop("PATH", None)
    return driver


def track_one(driver, tracking_number: str, courier: str = "") -> TrackResult:
    result = TrackResult(tracking_number=tracking_number, courier_input=courier)
    try:
        wait = WebDriverWait(driver, 20)
        navigate_to_tracking(driver, wait, tracking_number, courier)
        expand_shipment_timeline(driver)

        body_text = driver.find_element(By.TAG_NAME, "body").text
        result.raw_text = body_text

        status_line, journey = extract_status_and_history(driver, body_text)
        result.status = status_line
        result.last_update = journey_event_to_pipe(journey[0]) if journey else ""

    except Exception as exc:  # noqa: BLE001 - we want to record any failure
        result.error = f"{type(exc).__name__}: {exc}"

    return result


def save_debug(driver, result: TrackResult):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", result.tracking_number)
    txt_path = os.path.join(DEBUG_DIR, f"{safe_name}.txt")
    png_path = os.path.join(DEBUG_DIR, f"{safe_name}.png")
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(result.raw_text or "(empty - see error field)")
            if result.error:
                f.write(f"\n\n[ERROR] {result.error}")
        driver.save_screenshot(png_path)
    except Exception:
        pass


def load_tracking_numbers(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tn = (row.get("tracking_number") or "").strip()
            courier = (row.get("courier") or "").strip()
            if tn:
                rows.append((tn, courier))
    return rows


def main():
    jobs = load_tracking_numbers(INPUT_CSV)
    if not jobs:
        print(
            f"No tracking numbers found in {INPUT_CSV}. "
            "Add rows with columns: tracking_number,courier (courier optional)."
        )
        return

    driver = build_driver()
    results = []
    try:
        for tn, courier in jobs:
            print(f"Tracking {tn} ...")
            res = track_one(driver, tn, courier)
            save_debug(driver, res)
            results.append(res)
            status_preview = res.status or res.error or "(no status found - check debug/)"
            print(f"  -> {status_preview}")
    finally:
        driver.quit()

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["tracking_number", "courier_input", "status", "last_update", "error"]
        )
        for r in results:
            writer.writerow(
                [r.tracking_number, r.courier_input, r.status, r.last_update, r.error]
            )

    print(f"\nDone. Wrote {len(results)} result(s) to {OUTPUT_CSV}")
    print(f"Raw page text + screenshots saved under {DEBUG_DIR}/ for verification.")


if __name__ == "__main__":
    main()
