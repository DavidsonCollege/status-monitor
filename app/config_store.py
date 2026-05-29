"""Table Storage wrapper for the teams configuration.

The entire teams.json document is stored as a single entity
(PartitionKey="config", RowKey="current") with the JSON serialized into a
"data" property. ETags provide optimistic concurrency for admin edits.

NOTE: Azure Table Storage caps a single string property at 64 KiB. The current
config/teams.json is well under that, but if it grows past ~60 KiB the "data"
property must be chunked across multiple properties. Flagged in STATUS.md.
"""

from __future__ import annotations

import json
import os

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential

TABLE_NAME = "teamsConfig"
PARTITION_KEY = "config"
ROW_KEY = "current"


def _table_client() -> TableClient:
    account = os.environ["STORAGE_ACCOUNT_NAME"]
    endpoint = f"https://{account}.table.core.windows.net"
    return TableClient(
        endpoint=endpoint,
        table_name=TABLE_NAME,
        credential=DefaultAzureCredential(),
    )


class ConfigStore:
    def __init__(self) -> None:
        self._client = _table_client()

    def read(self) -> dict:
        """Return the config document, or an empty skeleton if unset."""
        try:
            entity = self._client.get_entity(PARTITION_KEY, ROW_KEY)
        except ResourceNotFoundError:
            return {"teams": []}
        return json.loads(entity.get("data") or '{"teams": []}')

    def read_with_etag(self) -> dict:
        """Return {"config": <dict>, "etag": <str|None>} for admin edits."""
        try:
            entity = self._client.get_entity(PARTITION_KEY, ROW_KEY)
        except ResourceNotFoundError:
            return {"config": {"teams": []}, "etag": None}
        return {
            "config": json.loads(entity.get("data") or '{"teams": []}'),
            "etag": entity.metadata.get("etag"),
        }

    def write(self, body: dict, etag: str | None = None) -> dict:
        """Persist the config. If etag is provided, enforce optimistic concurrency."""
        entity = {
            "PartitionKey": PARTITION_KEY,
            "RowKey": ROW_KEY,
            "data": json.dumps(body, ensure_ascii=False),
        }
        if etag:
            self._client.update_entity(
                entity,
                mode=UpdateMode.REPLACE,
                etag=etag,
                match_condition=MatchConditions.IfNotModified,
            )
        else:
            self._client.upsert_entity(entity, mode=UpdateMode.REPLACE)
        return {"ok": True}
