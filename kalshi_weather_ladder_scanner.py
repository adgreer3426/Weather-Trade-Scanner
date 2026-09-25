#!/usr/bin/env python3
"""
Scan Kalshi daily high/low temperature ladder markets for extreme rungs
trading at a given ask threshold (default 99¢). Reports the current and
next event per series, enriched with multi-model weather forecasts, sorted
so the strike furthest from the model consensus appears first.

Weather sources:
  NWS     – National Weather Service gridpoint forecast   (no key, US only)
  OM      – Open-Meteo seamless blend                     (no key, global)
  ECMWF   – ECMWF IFS via Open-Meteo                     (no key, 15-day)
  GFS     – NOAA GFS via Open-Meteo                       (no key, 16-day)
  GEM     – Environment Canada GEM via Open-Meteo         (no key, 10-day)
  VC      – Visual Crossing independent model blend       (free key, 15-day)
              → sign up at visualcrossing.com, then:
                export VISUAL_CROSSING_API_KEY="your-key"

  Distance column (Δ°) uses the mean of all non-None model values.

Setup:
    pip install -r requirements.txt
    export KALSHI_API_KEY_ID="your-api-key-id"
    export KALSHI_PRIVATE_KEY_PATH="/path/to/kalshi_private_key.pem"
    export VISUAL_CROSSING_API_KEY="your-key"   # optional but recommended

Usage:
    python3 kalshi_weather_ladder_scanner.py
    python3 kalshi_weather_ladder_scanner.py --threshold 99 --csv results.csv
    python3 kalshi_weather_ladder_scanner.py --series KXHIGHNY,KXLOWNY
    python3 kalshi_weather_ladder_scanner.py --dump-raw

Notes:
    Kalshi's series tickers shift over time (HIGHNY -> KXHIGHNY, etc.).
    The script auto-discovers weather ladder series from Kalshi's series
    list. If discovery comes up empty, pass --series with exact tickers
    from https://kalshi.com/hub/weather to keep it working without a
    code change.
"""

import argparse
import base64
import csv
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

try:
    from tabulate import tabulate as _tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

API_PREFIX = "/trade-api/v2"
DEFAULT_BASE_URL = "https://api.elections.kalshi.com"
DEMO_BASE_URL = "https://demo-api.kalshi.co"

WEATHER_TICKER_PREFIXES = ("KXHIGH", "KXLOW", "HIGHNY", "LOWNY")
WEATHER_TITLE_RE = re.compile(
    r"\b(high|low)\b.*\btemperature\b|\btemperature\b.*\b(high|low)\b", re.I
)
WEATHER_CATEGORY_RE = re.compile(r"weather|climate", re.I)
STRIKE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Short name -> Open-Meteo model identifier used in multi-model requests.
# Keys become column prefixes in row dicts (ecmwf_f, gfs_f, gem_f).
OPEN_METEO_MODELS = {
    "ecmwf": "ecmwf_ifs025",   # ECMWF IFS HRES — ~9 km, 15-day
    "gfs":   "gfs_global",     # NOAA GFS       — 0.25°,  16-day
    "gem":   "gem_global",     # Canada GEM      — 2.5 km, 10-day
}

# (display_name, nws_station, latitude, longitude)
# Keyed by the city suffix extracted from the series ticker after stripping
# the KXHIGH/KXLOW/KXHIGHT/KXLOWT prefix.  Coordinates are pinned to the
# official NWS observation station so forecasts match what Kalshi references.
CITY_MAP = {
    # Kalshi ticker suffix  display name            NWS stn   lat        lon
    "NY":   ("New York, NY",        "KNYC",  40.7789,  -73.9692),  # Central Park
    "NYC":  ("New York, NY",        "KNYC",  40.7789,  -73.9692),  # alias for KXLOWTNYC/KXHIGHNYC
    "CHI":  ("Chicago, IL",         "KMDW",  41.7868,  -87.7522),  # Midway
    "MIA":  ("Miami, FL",           "KMIA",  25.7959,  -80.2870),
    "LAX":  ("Los Angeles, CA",     "KLAX",  33.9425, -118.4081),
    "DEN":  ("Denver, CO",          "KDEN",  39.8561, -104.6737),
    "EMPDEN": ("Denver, CO",        "KDEN",  39.8561, -104.6737),  # alias for KXHIGHTEMPDEN
    "PHIL": ("Philadelphia, PA",    "KPHL",  39.8719,  -75.2411),
    "PHX":  ("Phoenix, AZ",         "KPHX",  33.4373, -112.0078),
    "MIN":  ("Minneapolis, MN",     "KMSP",  44.8820,  -93.2218),
    "DAL":  ("Dallas, TX",          "KDFW",  32.8998,  -97.0403),
    "NOLA": ("New Orleans, LA",     "KMSY",  29.9934,  -90.2580),
    "AUS":  ("Austin, TX",          "KAUS",  30.1945,  -97.6699),
    "SFO":  ("San Francisco, CA",   "KSFO",  37.6213, -122.3790),
    "HOU":  ("Houston, TX",         "KHOU",  29.6454,  -95.2789),  # Hobby
    "SATX": ("San Antonio, TX",     "KSAT",  29.5337,  -98.4698),
    "LV":   ("Las Vegas, NV",       "KLAS",  36.0840, -115.1537),
    "SEA":  ("Seattle, WA",         "KSEA",  47.4502, -122.3088),
    "OKC":  ("Oklahoma City, OK",   "KOKC",  35.3931,  -97.6007),
    "BOS":  ("Boston, MA",          "KBOS",  42.3631,  -71.0064),
    "DC":   ("Washington, DC",      "KDCA",  38.8521,  -77.0377),  # Reagan National
    "ATL":  ("Atlanta, GA",         "KATL",  33.6407,  -84.4277),
    "EWR":  ("Newark, NJ",          "KEWR",  40.6895,  -74.1745),
    "SAN":  ("San Diego, CA",       "KSAN",  32.7338, -117.1933),
    "KSAN": ("San Diego, CA",       "KSAN",  32.7338, -117.1933),  # ICAO alias for KXHIGHTKSAN
    "SDF":  ("Louisville, KY",      "KSDF",  38.1744,  -85.7360),
    "TTN":  ("Trenton, NJ",         "KTTN",  40.2767,  -74.8135),
}


# ---------------------------------------------------------------------------
# Kalshi API client
# ---------------------------------------------------------------------------

class KalshiClient:
    def __init__(self, key_id, private_key_path, base_url):
        self.key_id = key_id
        self.base_url = base_url.rstrip("/")
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms, method, path):
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def get(self, path, params=None):
        ts = str(int(time.time() * 1000))
        headers = {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, "GET", path),
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


# ---------------------------------------------------------------------------
# Series discovery
# ---------------------------------------------------------------------------

def discover_weather_series(client, dump_raw=False):
    try:
        series_list = client.paginate(f"{API_PREFIX}/series", items_key="series")
    except requests.HTTPError as exc:
        print(f"Warning: series discovery failed ({exc}); pass --series to skip.", file=sys.stderr)
        return []

    if dump_raw and series_list:
        print("--- sample series object ---", file=sys.stderr)
        print(series_list[0], file=sys.stderr)

    tickers = []
    for s in series_list:
        ticker = s.get("ticker", "")
        title = s.get("title", "") or ""
        category = s.get("category", "") or ""
        has_prefix   = ticker.upper().startswith(WEATHER_TICKER_PREFIXES)
        has_temp_title = bool(WEATHER_TITLE_RE.search(title))
        has_weather_cat = bool(WEATHER_CATEGORY_RE.search(category))
        # Require prefix + at least one weather signal (title or category) to
        # avoid pulling in non-temperature KXHIGH/KXLOW series like INFLATION,
        # MOVDJT, ESTRATE, etc. that share the prefix but aren't temp markets.
        is_daily_ladder = (has_prefix and (has_temp_title or has_weather_cat)) or \
                          (has_temp_title and has_weather_cat)
        # Only keep series we can fetch forecasts for; weekly (KXWEEK*),
        # national and international-airport series have no CITY_MAP entry.
        if is_daily_ladder and extract_city_code(ticker) in CITY_MAP:
            tickers.append(ticker)
    return sorted(set(tickers))


# ---------------------------------------------------------------------------
# Ticker helpers
# ---------------------------------------------------------------------------

def extract_city_code(series_ticker):
    # Some Kalshi tickers use KXHIGHT/KXLOWT (with an extra T) instead of
    # KXHIGH/KXLOW — the optional T? here absorbs that so PHX/MIN/etc. resolve
    # correctly to their CITY_MAP keys.
    m = re.match(r"^KX(?:HIGH|LOW)T?(.+)$", series_ticker, re.I)
    if m:
        return m.group(1).upper()
    m = re.match(r"^(?:HIGH|LOW)(.+)$", series_ticker, re.I)
    if m:
        return m.group(1).upper()
    return series_ticker.upper()


def series_type(series_ticker):
    return "HIGH" if re.search(r"HIGH", series_ticker, re.I) else "LOW"


def parse_event_date(event_ticker, series_ticker):
    """Return YYYY-MM-DD from event tickers like KXHIGHNY-26SEP24.
    Kalshi uses YYMMMDD format: two-digit year first, then month abbrev, then day.
    """
    suffix = event_ticker[len(series_ticker):].lstrip("-")
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{1,2})$", suffix, re.I)
    if m:
        yr  = int(m.group(1))
        mon = m.group(2).upper()
        day = int(m.group(3))
        month = MONTH_MAP.get(mon)
        if month:
            try:
                return date(2000 + yr, month, day).isoformat()
            except ValueError:
                pass
    return None


def strike_sort_key(market):
    # Brackets sort by midpoint; tails are nudged outward so "X or below"
    # always ranks below the lowest bracket and "X or above" above the
    # highest (their single strike can equal a bracket edge).
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    has_floor = isinstance(floor, (int, float))
    has_cap = isinstance(cap, (int, float))
    if has_floor and has_cap:
        return (float(floor) + float(cap)) / 2
    if has_cap:
        return float(cap) - 0.5
    if has_floor:
        return float(floor) + 0.5
    label = market.get("subtitle") or market.get("title") or market.get("ticker") or ""
    m = STRIKE_NUMBER_RE.search(label)
    return float(m.group()) if m else 0.0


def strike_value(market):
    """Return the numeric strike for distance calculations."""
    for field in ("cap_strike", "floor_strike"):
        v = market.get(field)
        if isinstance(v, (int, float)):
            return float(v)
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    m = STRIKE_NUMBER_RE.search(label)
    return float(m.group()) if m else None


RUNG_RANGE_RE = re.compile(r"(-?\d+)°?\s*to\s*(-?\d+)", re.I)
RUNG_TAIL_RE = re.compile(r"(-?\d+)°?\s*or\s*(above|below)", re.I)


def rung_bounds(market):
    """Return inclusive (lo, hi) °F that settles the rung YES; None = open end.
    Parsed from the label ("87° to 88°", "70° or above", "76° or below"),
    falling back to floor/cap strikes (floor-only = >floor, cap-only = <cap).
    """
    label = market.get("yes_sub_title") or market.get("subtitle") or ""
    m = RUNG_RANGE_RE.search(label)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = RUNG_TAIL_RE.search(label)
    if m:
        v = float(m.group(1))
        return (v, None) if m.group(2).lower() == "above" else (None, v)
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    has_floor = isinstance(floor, (int, float))
    has_cap = isinstance(cap, (int, float))
    if has_floor and has_cap:
        return float(floor), float(cap)
    if has_floor:
        return float(floor) + 1, None
    if has_cap:
        return None, float(cap) - 1
    return None


def rung_distance(bounds, temp):
    """Degrees from temp to the nearest edge of the rung's YES range (0 if inside)."""
    lo, hi = bounds
    if lo is not None and temp < lo:
        return lo - temp
    if hi is not None and temp > hi:
        return temp - hi
    return 0.0


def models_inside(bounds, model_temps):
    """Return names of models whose forecast falls inside the rung's YES range."""
    if bounds is None:
        return []
    lo, hi = bounds
    return [
        name for name, t in model_temps.items()
        if t is not None and (lo is None or t >= lo) and (hi is None or t <= hi)
    ]


# ---------------------------------------------------------------------------
# Weather data (NWS · Open-Meteo seamless · Open-Meteo multi-model · Visual Crossing)
# ---------------------------------------------------------------------------

def fetch_open_meteo(lat, lon, days=10):
    """Returns {YYYY-MM-DD: {high_f, low_f, precip_pct}} from OM seamless blend."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": days,
    }
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast?" + urlencode(params), timeout=15
    )
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    highs   = daily.get("temperature_2m_max", [])
    lows    = daily.get("temperature_2m_min", [])
    precips = daily.get("precipitation_probability_max", [])
    result = {}
    for i, dt in enumerate(daily.get("time", [])):
        result[dt] = {
            "high_f":    round(highs[i])   if i < len(highs)   and highs[i]   is not None else None,
            "low_f":     round(lows[i])    if i < len(lows)    and lows[i]    is not None else None,
            "precip_pct": int(precips[i])  if i < len(precips) and precips[i] is not None else None,
        }
    return result


def fetch_open_meteo_models(lat, lon, days=10):
    """Fetch ECMWF, GFS, and GEM temps in one Open-Meteo call (no API key).
    Returns {YYYY-MM-DD: {ecmwf_high_f, ecmwf_low_f, gfs_high_f, ...}}.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": days,
        "models": ",".join(OPEN_METEO_MODELS.values()),
    }
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast?" + urlencode(params), timeout=15
    )
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    dates = daily.get("time", [])
    result = {}
    for i, dt in enumerate(dates):
        day = {}
        for short, model_id in OPEN_METEO_MODELS.items():
            highs = daily.get(f"temperature_2m_max_{model_id}", [])
            lows  = daily.get(f"temperature_2m_min_{model_id}", [])
            day[f"{short}_high_f"] = round(highs[i]) if i < len(highs) and highs[i] is not None else None
            day[f"{short}_low_f"]  = round(lows[i])  if i < len(lows)  and lows[i]  is not None else None
        result[dt] = day
    return result


def fetch_visual_crossing(lat, lon, api_key, days=15):
    """Fetch Visual Crossing forecast (free tier: 1,000 records/day, 15-day horizon).
    Returns {YYYY-MM-DD: {high_f, low_f, precip_pct}}.
    Get a free key at https://www.visualcrossing.com/weather-api
    """
    url = (
        "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
        f"timeline/{lat:.4f},{lon:.4f}"
    )
    params = {
        "key": api_key,
        "unitGroup": "us",
        "include": "days",
        "elements": "datetime,tempmax,tempmin,precipprob",
        "contentType": "json",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    result = {}
    for day in resp.json().get("days", []):
        dt = day.get("datetime")
        if dt:
            result[dt] = {
                "high_f":     round(day["tempmax"])   if day.get("tempmax")    is not None else None,
                "low_f":      round(day["tempmin"])   if day.get("tempmin")    is not None else None,
                "precip_pct": int(day["precipprob"])  if day.get("precipprob") is not None else None,
            }
    return result


def fetch_nws(lat, lon):
    """Returns {YYYY-MM-DD: {high_f, low_f, precip_pct}}. US-only."""
    hdrs = {"User-Agent": "KalshiWeatherScanner/2.0 (weather-trade-scanner)"}
    try:
        r = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers=hdrs, timeout=10,
        )
        r.raise_for_status()
        grid_url = r.json()["properties"]["forecast"]
        r2 = requests.get(grid_url, headers=hdrs, timeout=10)
        r2.raise_for_status()
        periods = r2.json()["properties"]["periods"]
    except Exception:
        return {}

    result = {}
    for p in periods:
        dt = (p.get("startTime") or "")[:10]
        if not dt:
            continue
        is_day = p.get("isDaytime", True)
        temp = p.get("temperature")
        prob = None
        pp = p.get("probabilityOfPrecipitation")
        if isinstance(pp, dict) and pp.get("value") is not None:
            prob = int(pp["value"])

        if dt not in result:
            result[dt] = {}
        if is_day and "high_f" not in result[dt]:
            result[dt]["high_f"] = temp
            if prob is not None:
                result[dt].setdefault("precip_pct", prob)
        elif not is_day and "low_f" not in result[dt]:
            result[dt]["low_f"] = temp
            if prob is not None:
                result[dt].setdefault("precip_pct", prob)
    return result


def load_city_weather(city_code, cache, vc_key=None):
    """Fetch (and cache) all weather sources for a city code.
    Returns (nws, om, om_models, vc) — each is {date: {...}} or {}.
    """
    if city_code in cache:
        return cache[city_code]
    info = CITY_MAP.get(city_code)
    if info:
        city_name, station, lat, lon = info
        sources = "NWS · OM-seamless · OM-models(ECMWF/GFS/GEM)"
        if vc_key:
            sources += " · VisualCrossing"
        print(f"  {city_name} ({station}): {sources}", file=sys.stderr)
        try:
            nws = fetch_nws(lat, lon)
        except Exception as e:
            print(f"    NWS error: {e}", file=sys.stderr)
            nws = {}
        try:
            om = fetch_open_meteo(lat, lon)
        except Exception as e:
            print(f"    OM seamless error: {e}", file=sys.stderr)
            om = {}
        try:
            om_models = fetch_open_meteo_models(lat, lon)
        except Exception as e:
            print(f"    OM multi-model error: {e}", file=sys.stderr)
            om_models = {}
        vc = {}
        if vc_key:
            try:
                vc = fetch_visual_crossing(lat, lon, vc_key)
            except Exception as e:
                print(f"    Visual Crossing error: {e}", file=sys.stderr)
    else:
        print(f"  No station mapping for '{city_code}' — weather N/A", file=sys.stderr)
        nws, om, om_models, vc = {}, {}, {}, {}
    result = (nws, om, om_models, vc)
    cache[city_code] = result
    return result


# ---------------------------------------------------------------------------
# Scanning logic
# ---------------------------------------------------------------------------

def ask_cents(market, side):
    """Return ask price in whole cents (int) for 'yes' or 'no' side, or None."""
    raw = market.get(f"{side}_ask_dollars")
    if raw is None:
        return None
    try:
        return round(float(raw) * 100)
    except (ValueError, TypeError):
        return None


def find_extreme_rungs(markets, threshold):
    """Return [(position, side, market)] for top/bottom rungs with ask >= threshold."""
    if not markets:
        return []
    ranked = sorted(markets, key=strike_sort_key)
    hits = []
    for position, market in (("BOTTOM", ranked[0]), ("TOP", ranked[-1])):
        for side in ("yes", "no"):
            cents = ask_cents(market, side)
            # 100¢ asks have no upside, so they aren't actionable hits.
            if cents is not None and threshold <= cents < 100:
                hits.append((position, side.upper(), market))
    return hits


def scan_series(client, series_ticker, threshold, dump_raw_flag, weather_cache, vc_key=None):
    """Return list of result row dicts for the current and next events."""
    markets = client.paginate(
        f"{API_PREFIX}/markets",
        params={"series_ticker": series_ticker, "status": "open"},
    )
    if dump_raw_flag[0] and markets:
        print(f"--- sample market ({series_ticker}) ---", file=sys.stderr)
        print(markets[0], file=sys.stderr)
        dump_raw_flag[0] = False

    stype = series_type(series_ticker)
    city_code = extract_city_code(series_ticker)
    city_info = CITY_MAP.get(city_code)
    city_name = f"{city_info[0]} ({city_info[1]})" if city_info else city_code
    nws_data, om_data, om_models_data, vc_data = load_city_weather(
        city_code, weather_cache, vc_key=vc_key
    )

    by_event = defaultdict(list)
    for m in markets:
        by_event[m.get("event_ticker")].append(m)

    def event_sort_key(evt):
        d = parse_event_date(evt, series_ticker)
        return d or evt

    sorted_events = sorted(by_event.keys(), key=event_sort_key)
    temp_key = "high_f" if stype == "HIGH" else "low_f"

    rows = []
    for event_ticker in sorted_events[:2]:
        event_date = parse_event_date(event_ticker, series_ticker) or event_ticker
        hits = find_extreme_rungs(by_event[event_ticker], threshold)

        for position, side, market in hits:
            ask   = ask_cents(market, side.lower())
            label = (market.get("yes_sub_title")
                     or market.get("subtitle")
                     or market.get("title") or "")

            nws_day    = nws_data.get(event_date, {})
            om_day     = om_data.get(event_date, {})
            models_day = om_models_data.get(event_date, {})
            vc_day     = vc_data.get(event_date, {})

            hl = "high" if stype == "HIGH" else "low"
            nws_temp   = nws_day.get(temp_key)
            om_temp    = om_day.get(temp_key)
            ecmwf_temp = models_day.get(f"ecmwf_{hl}_f")
            gfs_temp   = models_day.get(f"gfs_{hl}_f")
            gem_temp   = models_day.get(f"gem_{hl}_f")
            vc_temp    = vc_day.get(temp_key)

            # Precipitation: prefer OM seamless, then VC, then NWS
            precip = (om_day.get("precip_pct")
                      if om_day.get("precip_pct") is not None
                      else (vc_day.get("precip_pct")
                            if vc_day.get("precip_pct") is not None
                            else nws_day.get("precip_pct")))

            # Distance from model consensus (mean of all available values) to
            # the nearest edge of the rung's YES range, e.g. 70 for "70° or above".
            bounds = rung_bounds(market)
            all_temps = [t for t in [nws_temp, om_temp, ecmwf_temp, gfs_temp, gem_temp, vc_temp]
                         if t is not None]
            consensus = round(sum(all_temps) / len(all_temps), 1) if all_temps else None
            if consensus is None:
                dist = None
            elif bounds is not None:
                dist = round(rung_distance(bounds, consensus), 1)
            else:
                sv = strike_value(market)
                dist = round(abs(consensus - sv), 1) if sv is not None else None

            inside = models_inside(bounds, {
                "NWS": nws_temp, "OM": om_temp, "ECMWF": ecmwf_temp,
                "GFS": gfs_temp, "GEM": gem_temp, "VC": vc_temp,
            })

            rows.append({
                "city":       city_name,
                "type":       stype,
                "date":       event_date,
                "strike":     label,
                "position":   position,
                "side":       side,
                "ask_cents":  ask,
                "nws_f":      nws_temp,
                "om_f":       om_temp,
                "ecmwf_f":    ecmwf_temp,
                "gfs_f":      gfs_temp,
                "gem_f":      gem_temp,
                "vc_f":       vc_temp,
                "precip_pct": precip,
                "dist":       dist,
                "inside":     ",".join(inside),
                "ticker":     market.get("ticker", ""),
            })
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt(val, fmt):
    return fmt.format(val) if val is not None else "N/A"


def render_table(rows):
    headers = [
        "City", "H/L", "Date", "Strike", "Pos", "Side",
        "Ask¢", "NWS°", "OM°", "ECMWF°", "GFS°", "GEM°", "VC°",
        "Rain%", "Δ° (avg)", "In rung", "Ticker",
    ]
    table = [
        [
            r["city"],
            r["type"],
            r["date"],
            r["strike"],
            r["position"],
            r["side"],
            r["ask_cents"],
            _fmt(r["nws_f"],      "{}°"),
            _fmt(r["om_f"],       "{}°"),
            _fmt(r["ecmwf_f"],    "{}°"),
            _fmt(r["gfs_f"],      "{}°"),
            _fmt(r["gem_f"],      "{}°"),
            _fmt(r["vc_f"],       "{}°"),
            _fmt(r["precip_pct"], "{}%"),
            _fmt(r["dist"],       "{}°"),
            f"⚠ {r['inside']}" if r["inside"] else "",
            r["ticker"],
        ]
        for r in rows
    ]

    if HAS_TABULATE:
        return _tabulate(table, headers=headers, tablefmt="simple")

    # Plain-text fallback
    col_widths = [
        max(len(str(h)), max((len(str(row[i])) for row in table), default=0))
        for i, h in enumerate(headers)
    ]
    sep = "  ".join("-" * w for w in col_widths)
    hdr = "  ".join(str(h).ljust(w) for h, w in zip(headers, col_widths))
    lines = [hdr, sep]
    for row in table:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--threshold", type=int, default=99,
        help="Ask price in cents to match (default: 99)",
    )
    parser.add_argument(
        "--series",
        help="Comma-separated series tickers to scan, skips auto-discovery",
    )
    parser.add_argument(
        "--demo", action="store_true", help="Use Kalshi demo environment",
    )
    parser.add_argument("--csv", help="Also write results to this CSV path")
    parser.add_argument(
        "--dump-raw", action="store_true",
        help="Print a sample raw series/market JSON object for debugging",
    )
    args = parser.parse_args()

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not private_key_path:
        print("Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH before running.", file=sys.stderr)
        sys.exit(1)

    base_url = (
        os.environ.get("KALSHI_API_BASE")
        or (DEMO_BASE_URL if args.demo else DEFAULT_BASE_URL)
    )
    client = KalshiClient(key_id, private_key_path, base_url)

    if args.series:
        series_tickers = [t.strip() for t in args.series.split(",") if t.strip()]
    else:
        series_tickers = discover_weather_series(client, dump_raw=args.dump_raw)
        if not series_tickers:
            print(
                "No weather ladder series discovered automatically.\n"
                "Pass --series KXHIGHNY,KXLOWNY,... with tickers from "
                "https://kalshi.com/hub/weather",
                file=sys.stderr,
            )
            sys.exit(1)

    vc_key = os.environ.get("VISUAL_CROSSING_API_KEY")
    if not vc_key:
        print(
            "Tip: set VISUAL_CROSSING_API_KEY for Visual Crossing forecasts "
            "(free at visualcrossing.com — VC° column will show N/A without it)",
            file=sys.stderr,
        )

    print(f"Scanning {len(series_tickers)} weather series: {', '.join(series_tickers)}", file=sys.stderr)

    weather_cache = {}
    dump_raw_flag = [args.dump_raw]
    all_rows = []
    for series_ticker in series_tickers:
        rows = scan_series(
            client, series_ticker, args.threshold, dump_raw_flag, weather_cache, vc_key=vc_key
        )
        all_rows.extend(rows)

    if not all_rows:
        print(f"\nNo extreme rungs found at {args.threshold}¢ ask in current/next events.")
        return

    # Sort: known-distance rows first, then by distance descending, then date/type/city
    all_rows.sort(
        key=lambda r: (
            0 if r["dist"] is not None else 1,
            -(r["dist"] or 0),
            r["date"],
            r["type"],
            r["city"],
        )
    )

    print(f"\n{len(all_rows)} extreme rung(s) at {args.threshold}¢ ask "
          f"(current + next events, furthest from forecast first):\n")
    print(render_table(all_rows))

    if args.csv:
        csv_fields = [
            "city", "type", "date", "strike", "position", "side",
            "ask_cents", "nws_f", "om_f", "ecmwf_f", "gfs_f", "gem_f", "vc_f",
            "precip_pct", "dist", "inside", "ticker",
        ]
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
