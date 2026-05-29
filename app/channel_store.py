"""Blob Storage wrapper for Slack/Zoom channel-name maps.

Each call to read/write hits a single JSON blob at
    feeds/channels/{kind}.json
inside the existing 'feeds' container — sharing the container avoids needing
new Bicep. The cron job writes via this store on each tick; the FastAPI app
reads via this store from /api/channels/{kind}; the dc-adminapi dispatcher
proxies that endpoint into its enrichment path.

Missing-blob is treated as "no map yet" and returns [] gracefully — the
dispatcher's existing fallback renders channel IDs raw when a name doesn't
resolve, so a first-deploy or transient write failure doesn't break the UI.
"""

from __future__ import annotations

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


class ChannelStore:
    def __init__(self) -> None:
        self._container = _container_client()

    @staticmethod
    def _blob_name(kind: str) -> str:
        return f"channels/{kind}.json"

    def read_channels(self, kind: str) -> list[dict]:
        try:
            data = self._container.download_blob(self._blob_name(kind)).readall()
        except ResourceNotFoundError:
            return []
        return json.loads(data)

    def write_channels(self, kind: str, channels: list[dict]) -> None:
        self._container.upload_blob(
            self._blob_name(kind),
            json.dumps(channels, ensure_ascii=False, indent=2),
            overwrite=True,
            content_type="application/json",
        )
