"""Fetch Slack channels via conversations.list, return a list of channel dicts.

Library-only — callers persist the result wherever they like (the Azure cron
writes to Blob via app.channel_store.ChannelStore). Requires the bot to have
channels:read and groups:read scopes.
"""

from __future__ import annotations

import time

import requests

SLACK_API_URL = "https://slack.com/api/conversations.list"


def fetch(token: str) -> list[dict]:
    """Return all Slack channels (public + private) accessible to the bot, as
    a list of {id, name, is_private, topic, num_members} sorted by lowercase
    name. Raises RuntimeError on empty token or a non-ok API response.
    """
    if not token:
        raise RuntimeError("Slack token is empty")

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "types": "public_channel,private_channel",
        "exclude_archived": "true",
        "limit": 100,
    }
    channels: list[dict] = []
    cursor = None
    retries = 5

    while True:
        if cursor:
            params["cursor"] = cursor

        for attempt in range(retries):
            try:
                resp = requests.get(SLACK_API_URL, headers=headers,
                                    params=params, timeout=15)
                data = resp.json()
                # Handle rate limiting
                if data.get("error") == "ratelimited":
                    wait = int(resp.headers.get("Retry-After", 10))
                    print(f"  Rate limited, waiting {wait}s (attempt {attempt + 1}/{retries})")
                    time.sleep(wait + 1)
                    continue
                break
            except Exception as exc:
                if attempt < retries - 1:
                    print(f"  Retry {attempt + 1}/{retries}: {exc}")
                    time.sleep(5)
                else:
                    raise

        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")

        for ch in data.get("channels", []):
            channels.append({
                "id": ch["id"],
                "name": ch.get("name", ""),
                "is_private": ch.get("is_private", False),
                "topic": (ch.get("topic") or {}).get("value", ""),
                "num_members": ch.get("num_members", 0),
            })

        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

        # Be polite between pages
        time.sleep(1)

    channels.sort(key=lambda c: c["name"].lower())
    return channels
