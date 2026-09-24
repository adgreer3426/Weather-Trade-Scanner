#!/bin/bash
# Cron wrapper for kalshi_99_ask_alert.py.
#
# Cron doesn't inherit your shell environment, so credentials live in a
# small env file OUTSIDE this repo (never commit API keys). Copy
# env.sample to /etc/kalshi/env, fill it in, then:
#   sudo chmod 600 /etc/kalshi/env
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source /etc/kalshi/env

exec python3 "$SCRIPT_DIR/kalshi_99_ask_alert.py" --email
