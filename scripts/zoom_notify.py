"""
Zoom Team Chat notifications for Status Monitor.

Sends formatted messages to per-team Zoom channels with color-coded sidebar
bars indicating severity:
  - Green (#2fcc66): Operational / Resolved
  - Yellow (#f1c40f): Degraded Performance / Monitoring
  - Orange (#e67e22): Partial Outage / Identified
  - Red (#e74c3c): Major Outage / Investigating
  - Blue (#3498db): Maintenance

Uses the Zoom Chatbot API (/v2/im/chat/messages) with client_credentials
OAuth to post as a named bot in Team Chat channels.

Required environment variables:
  ZOOM_CLIENT_ID          - App Client ID (from Zoom Marketplace)
  ZOOM_CLIENT_SECRET      - App Client Secret
  ZOOM_ACCOUNT_ID         - Zoom account ID
  ZOOM_BOT_JID            - Bot JID (from app Surface > Team Chat Subscription)
  ZOOM_USER_JID           - JID of a user who authorized the app
"""

import os
import json
import base64
from datetime import datetime, timezone

import requests

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_CHATBOT_URL = "https://api.zoom.us/v2/im/chat/messages"

CHANNEL_JID_SUFFIX = "@conference.xmpp.zoom.us"
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Status → sidebar colour
STATUS_COLORS = {
    "operational": "#2fcc66",
    "degraded": "#f1c40f",
    "partial_outage": "#e67e22",
    "major_outage": "#e74c3c",
    "maintenance": "#3498db",
    "unknown": "#95a5a6",
}

# Status → emoji for plain text fallback
STATUS_EMOJI_TEXT = {
    "operational": "🟢",
    "degraded": "🟡",
    "partial_outage": "🟠",
    "major_outage": "🔴",
    "maintenance": "🔵",
    "unknown": "⚪",
}


# ── Authentication ─────────────────────────────────────────────────────────────

def _get_chatbot_token() -> str:
    """Obtain a Zoom Chatbot token using client_credentials grant."""
    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("Missing ZOOM_CLIENT_ID or ZOOM_CLIENT_SECRET")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        ZOOM_OAUTH_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"No access_token in Zoom OAuth response: {data}")
    return token


# ── Chatbot API formatting ────────────────────────────────────────────────────

def _build_chatbot_body_element(event: dict) -> dict:
    product_name = event.get("product_name", "Unknown")
    title = event.get("title", "Status Update")
    summary = event.get("summary", "")
    link = event.get("link", "")
    status = event.get("status", "unknown")
    sidebar_color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])

    lines = [f"*{product_name}*"]
    if link:
        lines.append(f"*<{link}|{title}>*")
    else:
        lines.append(f"*{title}*")
    if summary:
        truncated = (summary[:300] + "…") if len(summary) > 300 else summary
        lines.append(truncated)
        if link:
            lines.append(f"<{link}|View status page →>")
    elif link:
        lines.append(f"<{link}|Status page →>")

    return {
        "type": "section",
        "sidebar_color": sidebar_color,
        "sections": [
            {
                "type": "message",
                "text": "\n".join(lines),
                "is_markdown_support": True,
            },
        ],
    }


def _build_chatbot_footer() -> dict:
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    return {
        "type": "message",
        "text": f"_{SEPARATOR}_\n_Updated {now}_",
        "is_markdown_support": True,
    }


def _build_chatbot_body(events: list[dict]) -> list[dict]:
    body = [_build_chatbot_body_element(evt) for evt in events]
    body.append(_build_chatbot_footer())
    return body


# ── Sending ────────────────────────────────────────────────────────────────────

def _to_channel_jid(channel_id: str) -> str:
    """Convert a plain channel ID to a full Zoom JID if needed."""
    if "@" in channel_id:
        return channel_id
    return f"{channel_id}{CHANNEL_JID_SUFFIX}"


def _send_message(channel_id: str, body: list[dict], token: str,
                  robot_jid: str, account_id: str, user_jid: str) -> bool:
    """Send a message via the Chatbot API."""
    to_jid = _to_channel_jid(channel_id)
    payload = {
        "robot_jid": robot_jid,
        "to_jid": to_jid,
        "user_jid": user_jid,
        "account_id": account_id,
        "is_markdown_support": True,
        "content": {
            "head": {"text": "Status Monitor"},
            "body": body,
        },
    }

    resp = requests.post(
        ZOOM_CHATBOT_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )

    if resp.status_code in (200, 201):
        return True

    # Debug: show payload fields (not secrets) to help diagnose
    print(f"  Zoom chatbot error ({to_jid}): {resp.status_code} {resp.text[:500]}")
    print(f"    robot_jid: {robot_jid}")
    print(f"    to_jid:    {to_jid}")
    print(f"    user_jid:  {user_jid}")
    print(f"    account_id length: {len(account_id)}")
    return False


def send_zoom_notifications(new_events: list[dict], base_url: str):
    """Send Zoom Team Chat notifications for status change events."""
    # Check required credentials
    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    robot_jid = os.environ.get("ZOOM_BOT_JID", "")
    account_id = os.environ.get("ZOOM_ACCOUNT_ID", "")
    user_jid = os.environ.get("ZOOM_USER_JID", "")

    if not client_id or not robot_jid:
        if new_events:
            print("  Zoom: missing ZOOM_CLIENT_ID or ZOOM_BOT_JID – skipping")
        return

    if not new_events:
        return

    # Group events by target channel
    by_channel: dict[str, list[dict]] = {}
    for event in new_events:
        channel = event.get("zoom_channel", "")
        if not channel:
            continue
        by_channel.setdefault(channel, []).append(event)

    if not by_channel:
        print("  No Zoom channels configured – skipping notifications")
        return

    # Get chatbot token
    try:
        token = _get_chatbot_token()
    except Exception as exc:
        print(f"  Zoom OAuth error: {exc}")
        return

    if not user_jid:
        print("  Zoom: WARNING – ZOOM_USER_JID not set, messages may fail")
    else:
        print(f"  Zoom: using Chatbot API (user_jid={user_jid})")

    # Send to each channel
    for channel_id, events in by_channel.items():
        try:
            body = _build_chatbot_body(events)
            ok = _send_message(channel_id, body, token, robot_jid,
                               account_id, user_jid)
            if ok:
                print(f"  Zoom: posted {len(events)} events to {channel_id}")
        except Exception as exc:
            print(f"  Zoom exception ({channel_id}): {exc}")
