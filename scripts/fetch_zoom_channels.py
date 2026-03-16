"""
Fetch Zoom Team Chat channels and save as a static JSON file for the dashboard.
Uses Server-to-Server OAuth credentials to call the Zoom chat channels API,
then writes docs/zoom_channels.json with channel metadata.
Requires scopes: chat_channel:read
"""

import json
import os
import base64
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# The ZOOM_ACCOUNT_ID secret has a typo (lowercase L instead of uppercase I
# at position 18).  Override it here until the secret is corrected.
if os.environ.get("ZOOM_ACCOUNT_ID", "") == "7JETkq73TWeDBpeGAvlH_g":
    os.environ["ZOOM_ACCOUNT_ID"] = "7JETkq73TWeDBpeGAvIH_g"

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_CHANNELS_URL = "https://api.zoom.us/v2/chat/users/me/channels"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "zoom_channels.json"


def get_access_token() -> str:
    """Obtain a Zoom Server-to-Server OAuth access token."""
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
        data={
            "grant_type": "account_credentials",
            "account_id": account_id,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"No access_token in Zoom OAuth response: {data}")
    return token


def fetch_channels(token: str) -> list[dict]:
    """Fetch all Zoom Team Chat channels with pagination."""
    headers = {"Authorization": f"Bearer {token}"}
    params = {"page_size": 50}
    channels = []
    next_page_token = ""
    retries = 3

    while True:
        if next_page_token:
            params["next_page_token"] = next_page_token

        for attempt in range(retries):
            try:
                resp = requests.get(ZOOM_CHANNELS_URL, headers=headers,
                                    params=params, timeout=15)
                # Handle rate limiting
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    print(f"  Rate limited, waiting {wait}s (attempt {attempt + 1}/{retries})")
                    time.sleep(wait + 1)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.exceptions.HTTPError:
                raise
            except Exception as exc:
                if attempt < retries - 1:
                    print(f"  Retry {attempt + 1}/{retries}: {exc}")
                    time.sleep(5)
                else:
                    raise

        for ch in data.get("channels", []):
            channels.append({
                "id": ch.get("id", ""),
                "name": ch.get("name", ""),
                "type": ch.get("type", 0),
                "members_count": ch.get("members", {}).get("total", 0)
                    if isinstance(ch.get("members"), dict)
                    else ch.get("members_count", 0),
            })

        next_page_token = data.get("next_page_token", "")
        if not next_page_token:
            break

        # Small delay between pages
        time.sleep(1)

    channels.sort(key=lambda c: c["name"].lower())
    return channels


def main():
    client_id = os.environ.get("ZOOM_CLIENT_ID", "")
    result = {
        "channels": [],
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "fetch_status": "success",
    }

    if not client_id:
        print("  ZOOM_CLIENT_ID not set -- writing stub zoom_channels.json")
        result["fetch_status"] = "no_token"
    else:
        try:
            token = get_access_token()
            result["channels"] = fetch_channels(token)
            print(f"  Fetched {len(result['channels'])} Zoom channels")
        except Exception as exc:
            print(f"  Error fetching Zoom channels: {exc}")
            result["fetch_status"] = f"error: {exc}"

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, indent=2) + "\n")
    print(f"  Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
