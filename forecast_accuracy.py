#!/usr/bin/env python3
"""
Backtest next-day high/low forecasts for every station in the scanner's
CITY_MAP and write per-city accuracy stats that the scanner shows in its
"Hist acc" column.

Truth is the NWS Daily Climate Report (CLI) — the same source Kalshi settles
on — whose climate day runs midnight to midnight local *standard* time.

Forecasts (all "issued the day before" the target date):
  NWS              – archived Point Forecast Matrix (IEM), latest issued in
                     the 24h before 15Z on the prior day (the morning package)
  OM/ECMWF/GFS/GEM – Open-Meteo Previous Runs API, temperature_2m_previous_day1,
                     max/min over the LST climate day

Visual Crossing is not included: historical forecasts need a paid plan.

The raw per-day data is kept in data/forecast_history.json so later runs only
fetch the last few weeks. Stats cover a rolling window (default 180 days).

Usage:
    python3 forecast_accuracy.py            # incremental update
    python3 forecast_accuracy.py --full     # rebuild the whole window
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from kalshi_weather_ladder_scanner import CITY_MAP

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.path.join(HERE, "data", "forecast_history.json")
ACCURACY_PATH = os.path.join(HERE, "data", "forecast_accuracy.json")

# Days re-fetched on every incremental run, so late or revised CLI reports and
# days missed by an earlier run get filled in.
REFETCH_DAYS = 14
# Fewest scored days a source needs before it can be named a city's best.
MIN_DAYS = 30

# station -> (NWS forecast office issuing the PFM, UTC offset of local standard time)
STATION_INFO = {
    "KNYC": ("OKX", -5), "KEWR": ("OKX", -5), "KPHL": ("PHI", -5), "KTTN": ("PHI", -5),
    "KBOS": ("BOX", -5), "KDCA": ("LWX", -5), "KATL": ("FFC", -5), "KMIA": ("MFL", -5),
    "KSDF": ("LMK", -5),
    "KMDW": ("LOT", -6), "KMSP": ("MPX", -6), "KDFW": ("FWD", -6), "KMSY": ("LIX", -6),
    "KAUS": ("EWX", -6), "KSAT": ("EWX", -6), "KHOU": ("HGX", -6), "KOKC": ("OUN", -6),
    "KDEN": ("BOU", -7), "KPHX": ("PSR", -7),
    "KLAX": ("LOX", -8), "KSFO": ("MTR", -8), "KLAS": ("VEF", -8), "KSEA": ("SEW", -8),
    "KSAN": ("SGX", -8),
}

# Source name -> Open-Meteo model id (names match the scanner's columns)
OM_MODELS = {"OM": "best_match", "ECMWF": "ecmwf_ifs025", "GFS": "gfs_global", "GEM": "gem_global"}
SOURCES = ["NWS", *OM_MODELS]

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "KalshiWeatherScanner/2.0 (weather-trade-scanner)"


def http_get(url, params=None):
    for attempt in range(5):
        try:
            resp = HTTP.get(url, params=params, timeout=120)
            if resp.status_code == 429:
                time.sleep(20)
                continue
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt == 4:
                raise
            time.sleep(5)


def stations():
    """{station: (lat, lon)} for every station the scanner knows about."""
    out = {}
    for _name, stn, lat, lon in CITY_MAP.values():
        out.setdefault(stn, (lat, lon))
    return out


# ---------------------------------------------------------------------------
# Observations: NWS CLI via IEM
# ---------------------------------------------------------------------------

def fetch_cli(stn, start, end):
    """{YYYY-MM-DD: (high, low)} observed."""
    out = {}
    for year in range(start.year, end.year + 1):
        data = http_get("https://mesonet.agron.iastate.edu/json/cli.py",
                        {"station": stn, "year": year}).json()
        for r in data.get("results", []):
            hi, lo = r.get("high"), r.get("low")
            if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
                out[r["valid"]] = (hi, lo)
    return out


# ---------------------------------------------------------------------------
# Model forecasts: Open-Meteo Previous Runs API
# ---------------------------------------------------------------------------

def fetch_models(lat, lon, utc_offset, start, end):
    """{YYYY-MM-DD: {source: (high, low)}} from day-ahead model runs."""
    hourly = http_get("https://previous-runs-api.open-meteo.com/v1/forecast", {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m_previous_day1",
        "models": ",".join(OM_MODELS.values()),
        "temperature_unit": "fahrenheit", "timezone": "GMT",
        "start_date": str(start - timedelta(days=1)), "end_date": str(end + timedelta(days=1)),
    }).json()["hourly"]
    local_days = [
        (datetime.fromisoformat(t) + timedelta(hours=utc_offset)).date().isoformat()
        for t in hourly["time"]
    ]
    out = defaultdict(dict)
    for src, model_id in OM_MODELS.items():
        by_day = defaultdict(list)
        for day, temp in zip(local_days, hourly.get(f"temperature_2m_previous_day1_{model_id}", [])):
            if temp is not None:
                by_day[day].append(temp)
        for day, temps in by_day.items():
            if len(temps) >= 22:  # skip partial days at the range edges
                out[day][src] = (max(temps), min(temps))
    return out


# ---------------------------------------------------------------------------
# NWS forecasts: archived Point Forecast Matrices via IEM
# ---------------------------------------------------------------------------

WMO_HEADER_RE = re.compile(r"^[A-Z0-9]{6} K[A-Z]{3} (\d{2})(\d{2})(\d{2})", re.M)


def fetch_pfm_text(wfo, start, end):
    """Raw text of every PFM<wfo> product issued in [start, end), in <=31-day chunks."""
    chunks = []
    lo = start
    while lo < end:
        hi = min(lo + timedelta(days=31), end)
        chunks.append(http_get("https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py", {
            "pil": f"PFM{wfo}", "sdate": str(lo), "edate": str(hi),
            "fmt": "text", "limit": 9999,
        }).text)
        lo = hi
    return "\n".join(chunks)


def parse_pfm_point(product, lat, lon):
    """{YYYY-MM-DD: {'high'|'low': temp}} for the PFM point nearest lat/lon, or {}.

    The first (3-hourly) section's Min/Max row puts each value under the
    column hour it applies to: morning columns hold the overnight low that
    ends that morning, evening columns hold that day's high.
    """
    best = None
    for block in product.split("$$"):
        m = re.search(r"^(\d+\.\d+)N\s+(\d+\.\d+)W", block, re.M)
        if m:
            dist = abs(float(m.group(1)) - lat) + abs(-float(m.group(2)) - lon)
            if dist < 0.12 and (best is None or dist < best[0]):
                best = (dist, block)
    if not best:
        return {}
    lines = best[1].splitlines()
    di = next((i for i, l in enumerate(lines) if l.startswith("Date")), None)
    if di is None or di + 2 >= len(lines):
        return {}
    first_date = re.search(r"(\d\d)/(\d\d)/(\d\d)", lines[di])
    minmax = next((l for l in lines[di:di + 6] if l.startswith(("Min/Max", "Max/Min"))), None)
    if not first_date or not minmax:
        return {}

    # Local-hour row: map each column's end position to (local date, hour)
    cur = date(2000 + int(first_date.group(3)), int(first_date.group(1)), int(first_date.group(2)))
    cols, prev_hour = {}, None
    local_row = lines[di + 1]
    for hm in re.finditer(r"\d\d", local_row[10:]):
        hour = int(hm.group())
        if prev_hour is not None and hour < prev_hour:
            cur += timedelta(days=1)
        prev_hour = hour
        cols[hm.end() + 10] = (cur, hour)

    out = defaultdict(dict)
    for vm in re.finditer(r"-?\d+", minmax[8:]):
        col = cols.get(vm.end() + 8)
        if col:
            out[col[0].isoformat()]["low" if col[1] < 12 else "high"] = int(vm.group())
    return out


def fetch_nws(wfo, lat, lon, start, end):
    """{YYYY-MM-DD: (high, low)} from the PFM issued the morning before each date."""
    text = fetch_pfm_text(wfo, start - timedelta(days=2), end + timedelta(days=1))
    headers = list(WMO_HEADER_RE.finditer(text))
    issued = []
    for m, nxt in zip(headers, headers[1:] + [None]):
        product = text[m.start():nxt.start() if nxt else len(text)]
        fc = parse_pfm_point(product, lat, lon)
        if not fc:
            continue
        # WMO header only has day-of-month; the product's first forecast date
        # is its issue date or at most a couple of days later.
        day, hour, minute = (int(g) for g in m.groups())
        first = date.fromisoformat(min(fc))
        for d in (first, first - timedelta(days=1), first - timedelta(days=2)):
            if d.day == day:
                issued.append((datetime(d.year, d.month, d.day, hour, minute, tzinfo=timezone.utc), fc))
                break
    issued.sort(key=lambda x: x[0])

    out = {}
    d = start
    while d <= end:
        key = d.isoformat()
        cutoff = datetime(d.year, d.month, d.day, 15, tzinfo=timezone.utc) - timedelta(days=1)
        latest = None
        for when, fc in issued:
            if cutoff - timedelta(hours=24) <= when <= cutoff and key in fc:
                latest = fc[key]
        if latest and "high" in latest and "low" in latest:
            out[key] = (latest["high"], latest["low"])
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# History + stats
# ---------------------------------------------------------------------------

def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def update_history(history, start, end):
    """Fetch [start, end] for every station and merge into history in place.
    history: {station: {date: {"obs": [hi, lo], source: [hi, lo], ...}}}
    """
    for stn, (lat, lon) in sorted(stations().items()):
        wfo, utc_offset = STATION_INFO[stn]
        print(f"  {stn}: fetching {start} .. {end}", file=sys.stderr)
        days = history.setdefault(stn, {})
        fetched = {}
        for label, fetch in (
            ("obs", lambda: {d: {"obs": v} for d, v in fetch_cli(stn, start, end).items()}),
            ("models", lambda: fetch_models(lat, lon, utc_offset, start, end)),
            ("NWS", lambda: {d: {"NWS": v} for d, v in fetch_nws(wfo, lat, lon, start, end).items()}),
        ):
            try:
                result = fetch()
            except requests.RequestException as e:
                print(f"    {label} failed: {e}", file=sys.stderr)
                continue
            for d, vals in result.items():
                if start.isoformat() <= d <= end.isoformat():
                    fetched.setdefault(d, {}).update({k: list(v) for k, v in vals.items()})
        for d, vals in fetched.items():
            days.setdefault(d, {}).update(vals)


def prune(history, window_start):
    for days in history.values():
        for d in [d for d in days if d < window_start.isoformat()]:
            del days[d]


def compute_accuracy(history):
    """{station: {"high"|"low": {"sources": {src: stats}, "best": src|None}}}"""
    out = {}
    for stn, days in history.items():
        out[stn] = {}
        for idx, kind in enumerate(("high", "low")):
            errors = defaultdict(list)
            for vals in days.values():
                obs = vals.get("obs")
                if not obs:
                    continue
                for src in SOURCES:
                    if vals.get(src):
                        errors[src].append(round(vals[src][idx]) - obs[idx])
            sources = {
                src: {
                    "n": len(e),
                    "mae": round(sum(abs(x) for x in e) / len(e), 2),
                    "bias": round(sum(e) / len(e), 2),
                    "within1": round(sum(abs(x) <= 1 for x in e) / len(e), 3),
                    "within2": round(sum(abs(x) <= 2 for x in e) / len(e), 3),
                }
                for src, e in errors.items() if e
            }
            eligible = [s for s in sources if sources[s]["n"] >= MIN_DAYS]
            best = min(eligible, key=lambda s: sources[s]["mae"]) if eligible else None
            out[stn][kind] = {"sources": sources, "best": best}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=180, help="Rolling window length (default 180)")
    parser.add_argument("--full", action="store_true", help="Re-fetch the whole window")
    args = parser.parse_args()

    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    window_start = end - timedelta(days=args.days - 1)

    history = {} if args.full else load_json(HISTORY_PATH)
    latest = max((d for days in history.values() for d in days), default=None)
    if latest:
        start = max(window_start, date.fromisoformat(latest) - timedelta(days=REFETCH_DAYS))
    else:
        start = window_start

    print(f"Updating forecast history {start} .. {end} (window from {window_start})", file=sys.stderr)
    update_history(history, start, end)
    prune(history, window_start)

    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, sort_keys=True, separators=(",", ":"))
    with open(ACCURACY_PATH, "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": [window_start.isoformat(), end.isoformat()],
            "stations": compute_accuracy(history),
        }, f, indent=1, sort_keys=True)
    print(f"Wrote {ACCURACY_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
