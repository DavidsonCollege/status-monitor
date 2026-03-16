"""
Zoom Team Chat notifications for Status Monitor.

Sends formatted messages to per-team Zoom channels with color-coded sidebar
bars indicating severity:
  - Green (#2fcc66): Operational / Resolved
  - Yellow (#f1c40f): Degraded Performance / Monitoring
  - Orange (#e67e22): Partial Outage / Identified
  - Red (#e74c3c): Major Outage / Investigating
  - Blue (#3498db): Maintenance

Supports two modes:
    1. Chatbot API — uses ZOOM_BOT_JID + client credentials
    2. User Chat API (fallback) — uses Server-to-Server OAuth
"""

import os
import json
import base64
from datetime import datetime, timezone

import requests

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_CHATBOT_URL = "https://api.zoom.us/v2/im/chat/messages"
ZOOM_CHAT_URL = "https://api.zoom.us/v2/chat/users/me/messages"

CHANNEL_JID_SUFFIX = "@conference.xmpp.zoom.us"
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Status → sidebar colour (matches Slack)
STATUS_COLORS = {
    "operational": "#2fcc66",
    "degraded": "#f1c40f",
    "partial_outage": "#e67e22",
    "major_outage": "#e74c3c",
    "maintenance": "#3498db",
    "unknown": "#95a5a6",
}

# Status → emoji for plain text messages
STATUS_EMOJI_TEXT = {
    "operational": "🟢",
    "degraded": "🟡",
    "partial_outage": "🟠",
    "major_outage": "🔴",
    "maintenance": "🔵",
    "unknown": "⚪",
}


# ── Authentication ─────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    client_secret = os.environ.get("ZOOM_CLIENT_SECRET", "")
    account_id = os.environ.get("ZOOM_ACCOUNT_ID", "")
    if not all([client_id, client_secret, account_id]):
        raise RuntimeError("Missing ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, or ZOOM_ACCOUNT_ID")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        ZOOM_OAUTH_URL,
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "account_credentials", "account_id": account_id},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("access_token", "")


def _get_chatbot_token() -> str:
    client_id = os.environ.get("ZOOM_CHATBOT_CLIENT_ID", "")
    client_secret = os.environ.get("ZOOM_CHATBOT_CLIENT_SECRET", "")
    if not all([client_id, client_secret]):
        raise RuntimeError("Missing ZOOM_CHATBOT_CLIENT_ID or ZOOM_CHATBOT_CLIENT_SECRET")

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
    return resp.json().get("access_token", "")


def _get_admin_user_jid(s2s_token: str) -> str:
    target_email = os.environ.get("ZOOM_USER_EMAIL", "")
    try:
        if target_email:
            resp = requests.get(
                f"https://api.zoom.us/v2/users/{target_email}",
                headers={"Authorization": f"Bearer {s2s_token}"},
                timeout=15,
            )
            resp.raise_for_status()
            user = resp.json()
            user_id = user.get("id", "")
            if user_id:
                jid = f"{user_id}@xmpp.zoom.us"
                print(f"    Resolved user_jid: {jid} ({user.get('email', target_email)})")
                return jid
        else:
            resp = requests.get(
                "https://api.zoom.us/v2/users",
                headers={"Authorization": f"Bearer {s2s_token}"},
                params={"page_size": 1, "status": "active"},
                timeout=15,
            )
            resp.raise_for_status()
            users = resp.json().get("users", [])
            if users:
                user_id = users[0].get("id", "")
                if user_id:
                    jid = f"{user_id}@xmpp.zoom.us"
                    print(f"    Resolved user_jid: {jid} ({users[0].get('email', '?')})")
                    return jid
    except Exception as exc:
        print(f"    Warning: could not fetch user JID: {exc}")
    return ""


# ── User Chat API formatting ──────────────────────────────────────────────────

def _build_user_chat_card(event: dict) -> str:
    product_name = event.get("product_name", "Unknown")
    title = event.get("title", "Status Update")
    summary = event.get("summary", "")
    link = event.get("link", "")
    status = event.get("status", "unknown")
    emoji = STATUS_EMOJI_TEXT.get(status, "⚪")

    lines = [f"{emoji} **{product_name}**", f"**{title}**"]
    if summary:
        truncated = (summary[:300] + "…") if len(summary) > 300 else summary
        lines.append(truncated)
    if link:
        lines.append(link)
    return "\n".join(lines)


def _build_user_chat_message(events: list[dict], base_url: str) -> str:
    cards = [_build_user_chat_card(evt) for evt in events]
    body = f"\n\n{SEPARATOR}\n\n".join(cards)
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    body += f"\n\n{SEPARATOR}\nUpdated {now}\n{base_url}"
    return body


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


def _build_chatbot_footer(base_url: str) -> dict:
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    return {
        "type": "message",
        "text": f"_{SEPARATOR}_\n_Updated {now} • <{base_url}|Status Monitor>_",
        "is_markdown_support": True,
    }


def _build_chatbot_body(events: list[dict], base_url: str) -> list[dict]:
    body = [_build_chatbot_body_element(evt) for evt in events]
    body.append(_build_chatbot_footer(base_url))
    return body


# ── Sending ────────────────────────────────────────────────────────────────────

def _to_channel_jid(channel_id: str) -> str:
    if "@" in channel_id:
        return channel_id
    return f"{channel_id}{CHANNEL_JID_SUFFIX}"


def _send_via_chatbot(channel_id, body, token, robot_jid, account_id, user_jid=""):
    to_jid = _to_channel_jid(channel_id)
    payload = {
        "robot_jid": robot_jid,
        "to_jid": to_jid,
        "user_jid": user_jid or robot_jid,
        "account_id": account_id,
        "is_markdown_support": True,
        "content": {
            "head": {"text": "Status Monitor"},
            "body": body,
        },
    }
    resp = requests.post(ZOOM_CHATBOT_URL,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json=payload, timeout=15)
    if resp.status_code in (200, 201):
        return True
    print(f"  Zoom chatbot error ({to_jid}): {resp.status_code} {resp.text[:500]}")
    return False


def _send_via_user_chat(channel_id, message, token):
    payload = {"message": message, "to_channel": channel_id}
    resp = requests.post(ZOOM_CHAT_URL,
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json=payload, timeout=15)
    if resp.status_code in (200, 201):
        return True
    print(f"  Zoom user-chat error ({channel_id}): {resp.status_code} {resp.text[:200]}")
    return False


def send_zoom_notifications(new_events: list[dict], base_url: str):
    """Send Zoom Team Chat notifications for status change events."""
    has_chatbot_creds = bool(os.environ.get("ZOOM_CHATBOT_CLIENT_ID", ""))
    has_s2s_creds = bool(os.environ.get("ZOOM_CLIENT_ID", ""))

    if not has_chatbot_creds and not has_s2s_creds:
        if new_events:
            print("  No Zoom credentials set – skipping Zoom notifications")
        return

    if not new_events:
        return

    by_channel: dict[str, list[dict]] = {}
    for event in new_events:
        channel = event.get("zoom_channel", "")
        if not channel:
            continue
        by_channel.setdefault(channel, []).append(event)

    if not by_channel:
        print("  No Zoom channels configured – skipping notifications")
        return

    robot_jid = os.environ.get("ZOOM_BOT_JID", "")
    account_id = os.environ.get("ZOOM_ACCOUNT_ID", "")
    use_chatbot = bool(robot_jid)

    try:
        if use_chatbot:
            token = _get_chatbot_token()
            # Prefer explicit ZOOM_USER_JID; fall back to S2S lookup
            admin_jid = os.environ.get("ZOOM_USER_JID", "")
            if not admin_jid and has_s2s_creds:
                try:
                    s2s_token = _get_access_token()
                    admin_jid = _get_admin_user_jid(s2s_token)
                except Exception:
                    pass
            if admin_jid:
                print(f"  Zoom: using Chatbot API (user_jid={admin_jid})")
            else:
                print("  Zoom: using Chatbot API (WARNING: no user_jid)")
        else:
            token = _get_access_token()
            print("  Zoom: using User Chat API")
    except Exception as exc:
        print(f"  Zoom OAuth error: {exc}")
        return

    for channel_id, events in by_channel.items():
        try:
            if use_chatbot:
                body = _build_chatbot_body(events, base_url)
                ok = _send_via_chatbot(channel_id, body, token, robot_jid, account_id, admin_jid)
            else:
                message = _build_user_chat_message(events, base_url)
                ok = _send_via_user_chat(channel_id, message, token)
            if ok:
                print(f"  Zoom: posted {len(events)} events to {channel_id}")
        except Exception as exc:
            print(f"  Zoom exception ({channel_id}): {exc}")
