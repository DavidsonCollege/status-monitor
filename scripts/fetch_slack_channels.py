"""
Fetch Slack channels and save as a static JSON file for the dashboard.
Uses the SLACK_BOT_TOKEN env var to call conversations.list,
then writes docs/slack_channels.json with channel metadata.
Requires bot scopes: channels:read, groups:read
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SLACK_API_URL = "https://slack.com/api/conversations.list"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "slack_channels.json"


def fetch_channels(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "types": "public_channel,private_channel",
        "exclude_archived": "true",
        "limit": 100,
    }
    channels = []
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

        # Small delay between pages to avoid rate limits
        time.sleep(1)

    channels.sort(key=lambda c: c["name"].lower())
    return channels


def main():
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    result = {
        "channels": [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "fetch_status": "success",
    }

    if not token:
        print("  SLACK_BOT_TOKEN not set -- writing stub slack_channels.json")
        result["fetch_status"] = "no_token"
    else:
        try:
            result["channels"] = fetch_channels(token)
            print(f"  Fetched {len(result['channels'])} Slack channels")
        except Exception as exc:
            print(f"  Error fetching Slack channels: {exc}")
            result["fetch_status"] = f"error: {exc}"

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2) + "\n")
    print(f"  Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
