"""Fetch Zoom Team Chat channels via the chat/users/me/channels API.

Library-only — callers persist the result wherever they like (the Azure cron
writes to Blob via app.channel_store.ChannelStore). Uses Server-to-Server OAuth
(scope: chat_channel:read) — credentials are passed in by the caller.
"""

from __future__ import annotations

import base64
import time

import requests

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_CHANNELS_URL = "https://api.zoom.us/v2/chat/users/me/channels"


def get_access_token(*, account_id: str, client_id: str, client_secret: str) -> str:
    """Obtain a Zoom Server-to-Server OAuth access token. Raises on missing creds
    or non-2xx response.
    """
    if not (account_id and client_id and client_secret):
        raise RuntimeError("Missing Zoom account_id, client_id, or client_secret")

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
    """Return all Zoom Team Chat channels accessible to the OAuth principal, as
    a list of {id, name, type, members_count} sorted by lowercase name.
    """
    headers = {"Authorization": f"Bearer {token}"}
    params: dict = {"page_size": 50}
    channels: list[dict] = []
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
                "members_count": (
                    ch.get("members", {}).get("total", 0)
                    if isinstance(ch.get("members"), dict)
                    else ch.get("members_count", 0)
                ),
            })

        next_page_token = data.get("next_page_token", "")
        if not next_page_token:
            break

        # Be polite between pages
        time.sleep(1)

    channels.sort(key=lambda c: c["name"].lower())
    return channels


def fetch(*, account_id: str, client_id: str, client_secret: str) -> list[dict]:
    """Convenience: OAuth + list in one call. Mirrors the slack module's API."""
    token = get_access_token(
        account_id=account_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    return fetch_channels(token)
