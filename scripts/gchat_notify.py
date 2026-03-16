"""
Google Chat webhook notifications for Status Monitor.

Sends Card v2 formatted messages to per-team Google Chat spaces via incoming
webhooks.  Each status event is rendered as a card with a color-coded header
indicating severity:
  - Green (#2fcc66): Operational / Resolved
  - Yellow (#f1c40f): Degraded Performance / Monitoring
  - Orange (#e67e22): Partial Outage / Identified
  - Red (#e74c3c): Major Outage / Investigating
  - Blue (#3498db): Maintenance

Setup:
  1. Open the Google Chat space where you want notifications.
  2. Click the space name → Apps & integrations → + Add webhooks.
  3. Give it a name (e.g. "Status Monitor") and optional avatar URL.
  4. Copy the webhook URL and add it to teams.json as "gchat_webhook".

No environment variables required — everything is configured in teams.json.
"""

import json
from datetime import datetime, timezone

import requests

# Status → colour hex (Google Chat header supports #RRGGBB)
STATUS_COLORS = {
    "operational": "#2fcc66",
    "degraded": "#f1c40f",
    "partial_outage": "#e67e22",
    "major_outage": "#e74c3c",
    "maintenance": "#3498db",
    "unknown": "#95a5a6",
}

# Status → human label
STATUS_LABELS = {
    "operational": "Operational",
    "degraded": "Degraded Performance",
    "partial_outage": "Partial Outage",
    "major_outage": "Major Outage",
    "maintenance": "Under Maintenance",
    "unknown": "Unknown",
}

# Status → emoji for visual scanning
STATUS_EMOJI = {
    "operational": "🟢",
    "degraded": "🟡",
    "partial_outage": "🟠",
    "major_outage": "🔴",
    "maintenance": "🔵",
    "unknown": "⚪",
}


# ── Card Building ────────────────────────────────────────────────────────────

def _build_event_card(event: dict, card_index: int) -> dict:
    """Build a Google Chat Card v2 for a single status event."""
    product_name = event.get("product_name", "Unknown")
    icon_url = event.get("icon_url", "")
    title = event.get("title", "Status Update")
    summary = event.get("summary", "")
    link = event.get("link", "")
    status = event.get("status", "unknown")
    emoji = STATUS_EMOJI.get(status, "⚪")
    status_label = STATUS_LABELS.get(status, "Unknown")

    # Card header with product name and status
    header = {
        "title": f"{emoji}  {product_name}",
        "subtitle": status_label,
    }
    if icon_url:
        header["imageUrl"] = icon_url
        header["imageType"] = "CIRCLE"

    # Body widgets
    widgets = []

    # Title widget
    widgets.append({
        "decoratedText": {
            "text": f"<b>{title}</b>",
            "wrapText": True,
        }
    })

    # Summary widget
    if summary:
        truncated = (summary[:400] + "…") if len(summary) > 400 else summary
        widgets.append({
            "textParagraph": {
                "text": truncated,
            }
        })

    # Link button
    if link:
        widgets.append({
            "buttonList": {
                "buttons": [{
                    "text": "View Status Page",
                    "onClick": {
                        "openLink": {"url": link}
                    }
                }]
            }
        })

    sections = [{"widgets": widgets}]

    return {
        "cardId": f"status-event-{card_index}",
        "card": {
            "header": header,
            "sections": sections,
        }
    }


def _build_footer_card(base_url: str, event_count: int, card_index: int) -> dict:
    """Build a footer card with timestamp and dashboard link."""
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    return {
        "cardId": f"status-footer-{card_index}",
        "card": {
            "sections": [{
                "widgets": [
                    {
                        "decoratedText": {
                            "text": f"<i>Updated {now}</i>",
                            "bottomLabel": f"{event_count} status update{'s' if event_count != 1 else ''}",
                        }
                    },
                    {
                        "buttonList": {
                            "buttons": [{
                                "text": "Open Status Dashboard",
                                "onClick": {
                                    "openLink": {"url": base_url}
                                }
                            }]
                        }
                    }
                ]
            }]
        }
    }


# ── Sending ──────────────────────────────────────────────────────────────────

def send_gchat_notifications(new_events: list[dict], base_url: str):
    """Send Google Chat webhook notifications for status change events."""
    if not new_events:
        return

    # Group events by target webhook URL
    by_webhook: dict[str, list[dict]] = {}
    for event in new_events:
        webhook_url = event.get("gchat_webhook", "")
        if not webhook_url:
            continue
        by_webhook.setdefault(webhook_url, []).append(event)

    if not by_webhook:
        print("  No Google Chat webhooks configured – skipping notifications")
        return

    for webhook_url, events in by_webhook.items():
        try:
            # Build cards — one per event plus a footer
            cards = []
            for i, evt in enumerate(events):
                cards.append(_build_event_card(evt, i))
            cards.append(_build_footer_card(base_url, len(events), len(events)))

            payload = {"cardsV2": cards}

            resp = requests.post(
                webhook_url,
                headers={"Content-Type": "application/json; charset=UTF-8"},
                json=payload,
                timeout=15,
            )

            if resp.status_code == 200:
                # Mask the webhook URL in logs for security
                masked = webhook_url[:60] + "…" if len(webhook_url) > 60 else webhook_url
                print(f"  Google Chat: posted {len(events)} events to {masked}")
            else:
                print(f"  Google Chat error: {resp.status_code} {resp.text[:500]}")

        except Exception as exc:
            print(f"  Google Chat exception: {exc}")
