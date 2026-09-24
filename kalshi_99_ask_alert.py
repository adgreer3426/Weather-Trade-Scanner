#!/usr/bin/env python3
"""
Pull every OPEN Kalshi daily high/low temperature market (every rung of
every city's ladder, not just the top/bottom) and report -- or email --
any market whose ask (YES or NO side) is sitting at a given threshold
(default 99 cents). Read-only market data only -- no positions or
order-placement endpoints are touched.

This is intentionally a separate, standalone script from
kalshi_weather_ladder_scanner.py (which only looks at the extreme rung of
each ladder). Duplicates a little client/discovery code on purpose so
each script can be deployed/cron'd independently.

Setup:
    pip install -r requirements.txt
    export KALSHI_API_KEY_ID="your-api-key-id"
    export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private_key.pem"

Usage:
    python3 kalshi_99_ask_alert.py                  # print hits to stdout
    python3 kalshi_99_ask_alert.py --email          # also email hits via
                                                     # the system `mail`
                                                     # command (msmtp)
    python3 kalshi_99_ask_alert.py --threshold 95
    python3 kalshi_99_ask_alert.py --series KXHIGHNY,KXLOWNY
    python3 kalshi_99_ask_alert.py --dump-raw

Intended to run unattended from cron on the homelab server -- see
crontab.sample and run_99_ask_alert.sh in this directory.
"""

import argparse
import base64
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_PREFIX = "/trade-api/v2"
DEFAULT_BASE_URL = "https://api.elections.kalshi.com"
DEMO_BASE_URL = "https://demo-api.kalshi.co"
DEFAULT_ALERT_EMAIL = "agreer26@gmail.com"

WEATHER_TICKER_PREFIXES = ("KXHIGH", "KXLOW", "HIGHNY", "LOWNY")
WEATHER_TITLE_RE = re.compile(r"\b(high|low)\b.*\btemperature\b|\btemperature\b.*\b(high|low)\b", re.I)
WEATHER_CATEGORY_RE = re.compile(r"weather|climate", re.I)


class KalshiClient:
    def __init__(self, key_id, private_key_path, base_url):
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms, method, path):
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def get(self, path, params=None):
        timestamp_ms = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self._sign(timestamp_ms, "GET", path),
        }
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def paginate(self, path, params=None, items_key="markets"):
        params = dict(params or {})
        items = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.get(path, params)
            items.extend(data.get(items_key, []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return items


def discover_weather_series(client, dump_raw=False):
    try:
        series_list = client.paginate(f"{API_PREFIX}/series", items_key="series")
    except requests.HTTPError as exc:
        print(f"Warning: series discovery failed ({exc}); pass --series to skip discovery.", file=sys.stderr)
        return []

    if dump_raw and series_list:
        print("--- sample series object ---", file=sys.stderr)
        print(series_list[0], file=sys.stderr)

    tickers = []
    for series in series_list:
        ticker = series.get("ticker", "")
        title = series.get("title", "") or ""
        category = series.get("category", "") or ""
        is_weather_prefix = ticker.upper().startswith(WEATHER_TICKER_PREFIXES)
        is_weather_title = bool(WEATHER_TITLE_RE.search(title)) and bool(WEATHER_CATEGORY_RE.search(category))
        if is_weather_prefix or is_weather_title:
            tickers.append(ticker)
    return sorted(set(tickers))


def find_ask_hits(markets, threshold):
    hits = []
    for market in markets:
        for side in ("yes", "no"):
            ask = market.get(f"{side}_ask")
            if ask == threshold:
                hits.append(
                    {
                        "series_ticker": market.get("series_ticker", ""),
                        "event_ticker": market.get("event_ticker", ""),
                        "ticker": market.get("ticker", ""),
                        "label": market.get("subtitle") or market.get("title") or "",
                        "side": side.upper(),
                        "ask_cents": ask,
                        "close_time": market.get("close_time", ""),
                    }
                )
    return hits


def format_hits(hits, threshold):
    lines = [f"{len(hits)} market(s) at {threshold}c ask:", ""]
    for hit in hits:
        lines.append(
            f"{hit['series_ticker']:<12} {hit['ticker']:<26} {hit['side']} ask={hit['ask_cents']}c  "
            f"\"{hit['label']}\"  closes {hit['close_time']}"
        )
    return "\n".join(lines)


def send_email(subject, body, to_addr):
    try:
        subprocess.run(["mail", "-s", subject, to_addr], input=body.encode("utf-8"), check=True)
    except FileNotFoundError:
        print(
            "Warning: `mail` command not found -- install mailutils and configure msmtp "
            "(see HomeNAS_Setup_Guide.md section 9.4) to enable --email.",
            file=sys.stderr,
        )
    except subprocess.CalledProcessError as exc:
        print(f"Warning: `mail` exited with an error ({exc}); check /var/log/msmtp.log.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--threshold", type=int, default=99, help="Ask price in cents to match (default: 99)")
    parser.add_argument("--series", help="Comma-separated series tickers to scan, skips auto-discovery")
    parser.add_argument("--demo", action="store_true", help="Use Kalshi's demo environment instead of production")
    parser.add_argument("--email", action="store_true", help="Email results via the system `mail` command if any hits are found")
    parser.add_argument("--to", default=os.environ.get("KALSHI_ALERT_EMAIL", DEFAULT_ALERT_EMAIL), help="Alert email recipient")
    parser.add_argument("--dump-raw", action="store_true", help="Print a sample raw series/market JSON object for debugging")
    args = parser.parse_args()

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not private_key_path:
        print("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH before running.", file=sys.stderr)
        sys.exit(1)

    base_url = os.environ.get("KALSHI_API_BASE") or (DEMO_BASE_URL if args.demo else DEFAULT_BASE_URL)
    client = KalshiClient(key_id, private_key_path, base_url)

    if args.series:
        series_tickers = [t.strip() for t in args.series.split(",") if t.strip()]
    else:
        series_tickers = discover_weather_series(client, dump_raw=args.dump_raw)
        if not series_tickers:
            print(
                "No weather ladder series discovered automatically. "
                "Pass --series KXHIGHNY,KXLOWNY,... with tickers from "
                "https://kalshi.com/hub/weather",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Scanning {len(series_tickers)} weather series: {', '.join(series_tickers)}", file=sys.stderr)

    all_hits = []
    dump_raw_remaining = args.dump_raw
    for series_ticker in series_tickers:
        markets = client.paginate(f"{API_PREFIX}/markets", params={"series_ticker": series_ticker, "status": "open"})
        if dump_raw_remaining and markets:
            print(f"--- sample market object ({series_ticker}) ---", file=sys.stderr)
            print(markets[0], file=sys.stderr)
            dump_raw_remaining = False
        all_hits.extend(find_ask_hits(markets, args.threshold))

    all_hits.sort(key=lambda h: (h["close_time"], h["series_ticker"]))

    if not all_hits:
        print(f"No markets found with ask == {args.threshold}c right now.")
        return

    report = format_hits(all_hits, args.threshold)
    print(report)

    if args.email:
        send_email(f"Kalshi weather alert: {len(all_hits)} market(s) at {args.threshold}c", report, args.to)


if __name__ == "__main__":
    main()
