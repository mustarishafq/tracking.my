# tracking.my lookup — web page + API

Local tool to check shipment status from https://www.tracking.my using
Selenium under the hood (no tracking.my account/API key needed). Runs as
a small web server with a search page and a JSON API.

## Setup

```
pip install -r requirements.txt
```

Requires Google Chrome installed on your machine. Selenium 4.6+ auto-downloads
the matching chromedriver, so no manual driver setup.

## Run

```
python app.py
```

Then open **http://localhost:5050** in your browser — type a tracking
number, hit Track. Each lookup opens a headless Chrome behind the scenes
and takes a few seconds.

## API

**Single lookup**

```
GET /api/track?tracking_number=EA123456789MY&courier=pos
```

`courier` is optional — tracking.my mostly auto-detects it from the
number format.

```bash
curl "http://localhost:5050/api/track?tracking_number=EA123456789MY"
```

Response:

```json
{
  "tracking_number": "EA123456789MY",
  "courier_input": "",
  "status": "...",
  "last_update": "...",
  "raw_text": "...full page text Selenium saw...",
  "error": ""
}
```

**Batch lookup**

```
POST /api/track/batch
Content-Type: application/json

{"items": [{"tracking_number": "EA123456789MY"}, {"tracking_number": "MY1234567890", "courier": "jt"}]}
```

```bash
curl -X POST http://localhost:5050/api/track/batch \
  -H "Content-Type: application/json" \
  -d '{"items":[{"tracking_number":"EA123456789MY"}]}'
```

Returns `{"results": [...]}` with one object per item, same shape as the
single-lookup response.

Note: requests are processed one at a time (Selenium isn't safe to run
concurrently against a single browser instance), so a batch of N numbers
takes roughly N × a few seconds.

## Known limitation — please read

I wasn't able to inspect tracking.my's live results page in this session
(browser inspection tool wasn't available), so `status` / `last_update`
extraction in `app.py` is a **heuristic** — keyword matching ("Delivered",
"In transit", etc., English + Bahasa Malaysia) plus date-pattern matching
over the page's visible text, not selectors confirmed against the real
result markup.

Every API response includes `raw_text` (the full page text Selenium saw)
and the search page shows it under "Raw page text (debug)". If `status`
comes back empty for a tracking number you know is valid, send me that
`raw_text` and I'll tighten the parser to match exactly.

## Better long-term option

tracking.my has an official "Tracking API" (webhook-based push updates,
free trial via https://seller.tracking.my). It's more reliable than
scraping since it won't break when the site's HTML changes and gives you
real-time push instead of polling. Worth it if this becomes a recurring/
production workflow rather than an on-demand check.

## Files

- `app.py` — the web page + API server (main entry point)
- `requirements.txt` — `pip install -r requirements.txt`
- `track_tracking_my.py` / `tracking_numbers.csv` — older CSV-driven CLI
  version, kept in case you ever want to run an offline batch without
  starting the server. Not needed for normal use.
