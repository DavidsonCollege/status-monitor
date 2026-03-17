"""
Slack bot notifications for Status Monitor.

Sends Block Kit formatted messages to per-team Slack channels using a bot
token with chat:write scope.  Each status event is rendered as a colored
attachment card — the sidebar colour indicates severity:
  - Green (#2fcc66): Operational / Resolved
  - Yellow (#f1c40f): Degraded Performance / Monitoring
  - Orange (#e67e22): Partial Outage / Identified
  - Red (#e74c3c): Major Outage / Investigating
  - Blue (#3498db): Maintenance
"""

import os
import json
from datetime import datetime, timezone

import requests

SLACK_API_URL = "https://slack.com/api/chat.postMessage"

# Status → sidebar colour
STATUS_COLORS = {
    "operational": "#2fcc66",
    "degraded": "#f1c40f",
    "partial_outage": "#e67e22",
    "major_outage": "#e74c3c",
    "maintenance": "#3498db",
    "unknown": "#95a5a6",
}

# Status → emoji for quick visual scanning
STATUS_EMOJI = {
    "operational": ":large_green_circle:",
    "degraded": ":large_yellow_circle:",
    "partial_outage": ":large_orange_circle:",
    "major_outage": ":red_circle:",
    "maintenance": ":large_blue_circle:",
    "unknown": ":white_circle:",
}


def send_slack_notifications(new_events: list[dict], base_url: str):
    """Send Slack Block Kit notifications for status change events."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    default_channel = os.environ.get("SLACK_DEFAULT_CHANNEL", "")

    if not token:
        if new_events:
            print("  SLACK_BOT_TOKEN not set – skipping Slack notifications")
        return

    if not new_events:
        return

    # Group events by target channel
    by_channel: dict[str, list[dict]] = {}
    for event in new_events:
        channel = event.get("slack_channel", "") or default_channel
        if not channel:
            continue
        by_channel.setdefault(channel, []).append(event)

    if not by_channel:
        print("  No Slack channels configured – skipping notifications")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    for channel, events in by_channel.items():
        attachments = _build_attachments(events, base_url)
        payload = {
            "channel": channel,
            "attachments": attachments,
            "text": f"{len(events)} status update{'s' if len(events) != 1 else ''}",
        }
        try:
            resp = requests.post(SLACK_API_URL, headers=headers, json=payload, timeout=10)
            data = resp.json()
            if data.get("ok"):
                print(f"  Slack: posted {len(events)} events to {channel}")
            else:
                print(f"  Slack error ({channel}): {data.get('error', resp.text)}")
        except Exception as exc:
            print(f"  Slack exception ({channel}): {exc}")


def _build_card_blocks(event: dict) -> list[dict]:
    """Build Block Kit blocks for a single status event."""
    product_name = event.get("product_name", "Unknown")
    icon_url = event.get("icon_url", "")
    title = event.get("title", "Status Update")
    summary = event.get("summary", "")
    link = event.get("link", "")
    status = event.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, ":white_circle:")

    blocks: list[dict] = []

    # Row 1: Product icon + name + status emoji
    if icon_url:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "image", "image_url": icon_url, "alt_text": product_name},
                {"type": "mrkdwn", "text": f"*{product_name}*  {emoji}"},
            ],
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{product_name}*  {emoji}"},
        })

    # Row 2: Title (bold, linked)
    if link:
        title_text = f"*<{link}|{title}>*"
    else:
        title_text = f"*{title}*"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": title_text},
    })

    # Row 3: Summary
    if summary:
        truncated = (summary[:400] + "…") if len(summary) > 400 else summary
        detail_text = truncated
        if link:
            detail_text += f"\n<{link}|View status page →>"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": detail_text},
        })
    elif link:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{link}|View status page →>"},
        })

    return blocks


def _build_attachments(events: list[dict], base_url: str) -> list[dict]:
    """Build Slack attachments — one colored card per event.

    Sidebar colour matches the event severity status.
    """
    attachments: list[dict] = []
    for event in events:
        status = event.get("status", "unknown")
        color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
        attachments.append({
            "color": color,
            "blocks": _build_card_blocks(event),
        })

    # Footer attachment
    attachments.append({
        "color": "#e0e0e0",
        "blocks": [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Updated {datetime.now(timezone.utc).strftime('%b %d, %Y %H:%M UTC')}",
                    }
                ],
            }
        ],
    })
    return attachments
