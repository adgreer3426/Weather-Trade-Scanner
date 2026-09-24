# Weather Trade Scanner

Two independent scripts for scanning Kalshi's daily high/low temperature
markets.
They share no code on purpose so each can be run/deployed separately.

1. `kalshi_weather_ladder_scanner.py` — flags the top/bottom rung of each
   city's ladder (documented below).
2. `kalshi_99_ask_alert.py` — flags *any* rung (every open market) at a
   given ask threshold, meant to run unattended from cron and email you.
   See [Script 2](#script-2-kalshi_99_ask_alertpy--any-rung-cron--email)
   below.

## Script 1: kalshi_weather_ladder_scanner.py — ladder extremes

Scans Kalshi's daily high/low temperature "ladder" markets and flags the
top or bottom rung of each city's ladder when its ask price (YES or NO
side) hits a threshold — 99 cents by default, i.e. the market considers
that extreme outcome a near-lock.

### Setup

1. Generate an RSA key pair and upload the public key in the Kalshi
   dashboard (Settings -> API Keys) to get an API Key ID:

   ```
   openssl genrsa -out kalshi_private_key.pem 4096
   openssl rsa -in kalshi_private_key.pem -pubout -out kalshi_public_key.pem
   ```

2. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

3. Set credentials (don't commit these):

   ```
   export KALSHI_API_KEY_ID="your-api-key-id"
   export KALSHI_PRIVATE_KEY_PATH="/secure/path/kalshi_private_key.pem"
   ```

### Usage

```
python3 kalshi_weather_ladder_scanner.py
python3 kalshi_weather_ladder_scanner.py --threshold 99 --csv results.csv
python3 kalshi_weather_ladder_scanner.py --series KXHIGHNY,KXLOWNY
python3 kalshi_weather_ladder_scanner.py --demo
```

Run it any time — via cron, or by hand — to check current ladders.

### If Kalshi changes ticker naming

The script auto-discovers weather series from Kalshi's `/series` endpoint
by matching ticker prefixes and category/title text. Kalshi has renamed
weather ticker prefixes before (e.g. legacy `HIGHNY` -> `KXHIGHNY`). If a
future rename makes auto-discovery come up empty, pass the exact tickers
manually with `--series`, found at https://kalshi.com/hub/weather.

Use `--dump-raw` to print a sample raw series/market JSON object to
stderr — handy if Kalshi changes field names (e.g. `floor_strike`) and
you need to adjust `strike_sort_key()` in the script.

## Script 2: kalshi_99_ask_alert.py — any rung, cron + email

Pulls every OPEN market in every discovered weather series (every rung of
every city's ladder, not just the extremes) and reports/emails any market
whose YES or NO ask is sitting at a threshold — 99 cents by default. No
positions or trading endpoints are touched, read-only market data only.

Meant to run unattended on a server via cron and alert you by email. It
shells out to the system `mail` command, so it reuses whatever mail
pipeline you already have configured (e.g. `msmtp` + `mailutils` on
Debian) rather than needing its own SMTP credentials.

### One-time setup on the server

```
pip install -r requirements.txt
sudo mkdir -p /etc/kalshi
sudo cp env.sample /etc/kalshi/env
sudo nano /etc/kalshi/env          # fill in your API key ID + private key path
sudo chmod 600 /etc/kalshi/env
chmod +x run_99_ask_alert.sh
```

### Run it by hand

```
python3 kalshi_99_ask_alert.py                # print hits to stdout
python3 kalshi_99_ask_alert.py --email        # also email hits if any found
python3 kalshi_99_ask_alert.py --threshold 95
python3 kalshi_99_ask_alert.py --series KXHIGHNY,KXLOWNY
```

Email only goes out when at least one market matches the threshold — no
noise when nothing's hit.

### Schedule it (4x/day, phone-friendly)

Install the four cron lines in `crontab.sample` (6:00am, 10:15am, 3:00pm,
5:00pm Central by default — edit the times to change frequency, no code
change needed). Results land in your inbox, so you can check from your
phone without SSHing in or opening a laptop.

### If Kalshi changes ticker naming or field names

Same as Script 1 — pass `--series` to bypass discovery, and `--dump-raw`
to inspect the raw JSON if `yes_ask`/`no_ask` field names ever change.

## Security note

Never commit your private key, API key ID, or `/etc/kalshi/env` to this
repo. Keep them in environment variables or a local, gitignored file
outside version control.
