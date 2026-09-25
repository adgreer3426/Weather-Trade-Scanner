#!/usr/bin/env python3
"""Email the scanner output (scan.txt, scan.log, results.csv) via Gmail SMTP.

Used by .github/workflows/weather-scan.yml. Needs GMAIL_ADDRESS and
GMAIL_APP_PASSWORD (a Google app password, not the account password).
Sends to GMAIL_ADDRESS unless EMAIL_TO is set.
"""

import html
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo


def read(path):
    p = Path(path)
    return p.read_text(errors="replace") if p.exists() else ""


def main():
    sender = os.environ["GMAIL_ADDRESS"]
    recipient = os.environ.get("EMAIL_TO") or sender

    table = read("scan.txt").strip()
    log = read("scan.log").strip()
    exit_code = read("exit_code").strip() or "?"
    csv_rows = max(len(read("results.csv").splitlines()) - 1, 0)

    stamp = datetime.now(ZoneInfo("America/Chicago")).strftime("%b %d %-I:%M%p CT")
    if exit_code != "0":
        subject = f"Kalshi weather scan FAILED — {stamp}"
    elif csv_rows:
        subject = f"Kalshi weather scan: {csv_rows} hit(s) — {stamp}"
    else:
        subject = f"Kalshi weather scan: no hits — {stamp}"

    body = table or "(no output)"
    if exit_code != "0":
        body += f"\n\nScanner exited with code {exit_code}. Log:\n\n{log}"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    # Monospace HTML part so the table columns line up in Gmail.
    msg.add_alternative(
        '<pre style="font-family:Menlo,Consolas,monospace;font-size:12px">'
        f"{html.escape(body)}</pre>",
        subtype="html",
    )
    if csv_rows:
        msg.add_attachment(
            read("results.csv").encode(), maintype="text", subtype="csv",
            filename=f"kalshi_scan_{datetime.now():%Y-%m-%d_%H%M}.csv",
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)
    print(f"Sent '{subject}' to {recipient}")


if __name__ == "__main__":
    main()
