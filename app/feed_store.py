"""Blob Storage wrapper for rendered status feeds.

Each team has two JSON blobs in the "feeds" container:
  - <team_id>.json          — the rolling event feed
  - <team_id>-status.json   — the current status summary for the dashboard

The web app reads these to serve /api/feeds/{teamId}; the cron job writes them.
"""

import json
import os

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

CONTAINER = "feeds"


def _container_client():
    account = os.environ["STORAGE_ACCOUNT_NAME"]
    endpoint = f"https://{account}.blob.core.windows.net"
    service = BlobServiceClient(endpoint, credential=DefaultAzureCredential())
    return service.get_container_client(CONTAINER)


class FeedStore:
    def __init__(self) -> None:
        self._container = _container_client()

    def _read_json(self, blob_name: str, default):
        try:
            data = self._container.download_blob(blob_name).readall()
        except ResourceNotFoundError:
            return default
        return json.loads(data)

    def _write_json(self, blob_name: str, value) -> None:
        self._container.upload_blob(
            blob_name,
            json.dumps(value, ensure_ascii=False, indent=2),
            overwrite=True,
            content_type="application/json",
        )

    def read_feed(self, team_id: str) -> list[dict]:
        return self._read_json(f"{team_id}.json", [])

    def write_feed(self, team_id: str, feed: list[dict]) -> None:
        self._write_json(f"{team_id}.json", feed)

    def read_summary(self, team_id: str) -> dict:
        return self._read_json(f"{team_id}-status.json", {})

    def write_summary(self, team_id: str, summary: dict) -> None:
        self._write_json(f"{team_id}-status.json", summary)
