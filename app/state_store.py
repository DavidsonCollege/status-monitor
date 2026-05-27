"""Table Storage wrappers for per-product state and change requests.

Two concerns live here, matching the migration plan:

  StateStore   — the checker's run state. read()/write() present the same
                 nested dict the file-based checker used:
                     {"_seen_updates": [...], "<team>": {"<product>": {...}}}
                 Storage layout (to respect Table Storage size limits):
                   - one entity per product: PK="product", RK="<team>::<product>",
                     "data" = JSON of that product's state
                   - seen-update IDs: PK="meta", RK="seen_updates", chunked
                     across "c0","c1",... properties (each < 32 KiB) since a
                     single property caps at 64 KiB and the list holds up to 5000 IDs

  ChangeStore  — public change-request submissions and their approval state.
                 PK="change", RK=<uuid>, status in {pending, approved, rejected}.
"""

import json
import os
import uuid
from datetime import datetime, timezone

from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential

STATE_TABLE = "state"
CHANGES_TABLE = "changeRequests"

SEEN_PK = "meta"
SEEN_RK = "seen_updates"
PRODUCT_PK = "product"
MAX_SEEN = 5000
_CHUNK_CHARS = 30000  # stay safely under the 64 KiB-per-property cap


def _table_client(table_name: str) -> TableClient:
    account = os.environ["STORAGE_ACCOUNT_NAME"]
    endpoint = f"https://{account}.table.core.windows.net"
    return TableClient(
        endpoint=endpoint,
        table_name=table_name,
        credential=DefaultAzureCredential(),
    )


def _chunk(text: str, size: int) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class StateStore:
    def __init__(self) -> None:
        self._client = _table_client(STATE_TABLE)

    def read(self) -> dict:
        state: dict = {"_seen_updates": []}
        for entity in self._client.list_entities():
            pk = entity["PartitionKey"]
            rk = entity["RowKey"]
            if pk == SEEN_PK and rk == SEEN_RK:
                joined = "".join(
                    entity[k] for k in sorted(entity.keys()) if k.startswith("c")
                )
                state["_seen_updates"] = json.loads(joined or "[]")
            elif pk == PRODUCT_PK and "::" in rk:
                team_id, product_id = rk.split("::", 1)
                state.setdefault(team_id, {})[product_id] = json.loads(
                    entity.get("data") or "{}"
                )
        return state

    def write(self, state: dict) -> None:
        seen = list(state.get("_seen_updates", []))[-MAX_SEEN:]
        chunks = _chunk(json.dumps(seen), _CHUNK_CHARS)
        seen_entity = {"PartitionKey": SEEN_PK, "RowKey": SEEN_RK}
        for i, chunk in enumerate(chunks):
            seen_entity[f"c{i}"] = chunk
        self._client.upsert_entity(seen_entity, mode=UpdateMode.REPLACE)

        for team_id, products in state.items():
            if team_id == "_seen_updates":
                continue
            for product_id, product_state in products.items():
                self._client.upsert_entity(
                    {
                        "PartitionKey": PRODUCT_PK,
                        "RowKey": f"{team_id}::{product_id}",
                        "data": json.dumps(product_state, ensure_ascii=False),
                    },
                    mode=UpdateMode.REPLACE,
                )


class ChangeStore:
    def __init__(self) -> None:
        self._client = _table_client(CHANGES_TABLE)

    def create(self, payload: dict) -> dict:
        change_id = uuid.uuid4().hex
        entity = {
            "PartitionKey": "change",
            "RowKey": change_id,
            "status": "pending",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "data": json.dumps(payload, ensure_ascii=False),
        }
        self._client.create_entity(entity)
        return {"id": change_id, "status": "pending"}

    def get(self, change_id: str) -> dict:
        try:
            entity = self._client.get_entity("change", change_id)
        except ResourceNotFoundError:
            raise KeyError(change_id)
        return {
            "id": entity["RowKey"],
            "status": entity.get("status"),
            "submitted_at": entity.get("submitted_at"),
            "data": json.loads(entity.get("data") or "{}"),
        }

    def list(self, status: str | None = "pending") -> list[dict]:
        out: list[dict] = []
        for entity in self._client.list_entities():
            if status and entity.get("status") != status:
                continue
            out.append(
                {
                    "id": entity["RowKey"],
                    "status": entity.get("status"),
                    "submitted_at": entity.get("submitted_at"),
                    "data": json.loads(entity.get("data") or "{}"),
                }
            )
        return out

    def set_status(self, change_id: str, status: str) -> dict:
        try:
            entity = self._client.get_entity("change", change_id)
        except ResourceNotFoundError:
            raise KeyError(change_id)
        entity["status"] = status
        self._client.update_entity(entity, mode=UpdateMode.MERGE)
        return {"id": change_id, "status": status}
