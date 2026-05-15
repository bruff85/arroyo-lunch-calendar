#!/usr/bin/env python3
"""
Gmail notification module for lunch calendar scripts.
Sends success/failure/not-found emails via Gmail API using OAuth credentials.

Notification logic:
- Found & Success: sent immediately when next month is loaded
- Found & Failure: sent immediately when month is found but processing fails
- Not Found: sent only on the 6pm PT run (02:00 UTC) if month still not published
"""

import os
import base64
from email.mime.text import MIMEText
from datetime import datetime, timezone

import requests


def get_access_token():
    """Get a fresh access token using the refresh token."""
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("Missing Gmail credentials in environment variables.")

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30
    )
    response.raise_for_status()
    return response.json()["access_token"]


def send_email(subject, body):
    """Send an email via Gmail API."""
    to_email = os.environ.get("NOTIFY_EMAIL")
    if not to_email:
        print("  No NOTIFY_EMAIL set — skipping email notification.")
        return

    try:
        access_token = get_access_token()

        message = MIMEText(body, "plain")
        message["to"] = to_email
        message["from"] = to_email
        message["subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        response = requests.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
            timeout=30
        )
        response.raise_for_status()
        print(f"  Email notification sent to {to_email} ✅")

    except Exception as e:
        print(f"  Failed to send email notification: {e}")


def is_evening_run():
    """
    Returns True if this is the 6pm PT run (02:00 UTC).
    The 7am PT run is at 14:00 UTC and 10am PT run is at 18:00 UTC.
    We only send 'not found' on the evening run.
    """
    now_utc = datetime.now(timezone.utc)
    # Evening run is at 02:xx UTC (6pm PT)
    # Morning runs are at 14:xx UTC (7am PT) and 18:xx UTC (10am PT)
    return now_utc.hour == 2


def notify_success(calendar_name, month_label, event_count):
    """Send a success notification email — sent immediately."""
    subject = f"✅ {calendar_name} — {month_label} Menu Loaded"
    body = f"""Hello,

The {calendar_name} has been successfully updated!

Month: {month_label}
Events generated: {event_count} school days

Your calendar subscription will refresh automatically within a few hours.

Subscription links:
- LCUSD: https://bruff85.github.io/graphql-snf-pipeline/lunch.ics
- Arroyo: https://bruff85.github.io/healthepro-pipeline/lunch.ics

— Lunch Calendar Bot
{datetime.now().strftime("%B %d, %Y at %I:%M %p")}
"""
    send_email(subject, body)


def notify_found_failure(calendar_name, month_label, reason):
    """Send a failure notification when month was found but processing failed — sent immediately."""
    subject = f"❌ {calendar_name} — {month_label} Found But Failed"
    body = f"""Hello,

The {calendar_name} found the {month_label} menu but encountered an error while processing it.

Error: {reason}

Please check the GitHub Actions log for details and consider triggering a manual run.

GitHub Actions:
- LCUSD: https://github.com/bruff85/graphql-snf-pipeline/actions
- Arroyo: https://github.com/bruff85/healthepro-pipeline/actions

— Lunch Calendar Bot
{datetime.now().strftime("%B %d, %Y at %I:%M %p")}
"""
    send_email(subject, body)


def notify_not_found(calendar_name, month_label):
    """
    Send a 'not found' notification — only sent on the evening run (6pm PT).
    This prevents duplicate emails when both the 10am and 6pm runs fail.
    """
    if not is_evening_run():
        print(f"  Morning run — skipping 'not found' email (will send tonight if still missing).")
        return

    subject = f"⏳ {calendar_name} — {month_label} Not Published Yet"
    body = f"""Hello,

The {calendar_name} checked for the {month_label} menu today but it has not been published yet.

The script will automatically retry tomorrow at 10am and 6pm PT.
You will receive this email once per day until the menu is found.

Once found you will receive a separate confirmation email.

GitHub Actions:
- LCUSD: https://github.com/bruff85/graphql-snf-pipeline/actions
- Arroyo: https://github.com/bruff85/healthepro-pipeline/actions

— Lunch Calendar Bot
{datetime.now().strftime("%B %d, %Y at %I:%M %p")}
"""
    send_email(subject, body)
