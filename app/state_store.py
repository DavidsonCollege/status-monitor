"""Table Storage wrappers for per-product state and change requests.

Two concerns live here, matching the migration plan:

  StateStore   — the checker's run state. read()/write() present the same
                 nested dict the file-based checker used:
                     {"_seen_updates": [...], "<team>": {"<product>": {...}}}
                 Storage layout (to respect Table Storage size limits):
                   - one entity per product: PK="product", RK="<team>::<product>",
                     JSON of the product's state CHUNKED across c0/c1/...
                     properties (each <= 32,000 UTF-8 bytes, safely under the
                     64 KiB-per-property cap).
                   - seen-update IDs: PK="meta", RK="seen_updates", JSON of the
                     up-to-5000 IDs chunked the same way.
                 Reads tolerate either the new chunked layout or the legacy
                 single "data" property (pre-chunking writes); a corrupt or
                 incomplete row is logged and the product's state is regenerated.

                 NOTE: chunking solves the per-property 64 KiB cap. Each entity
                 has a SEPARATE ~1 MB total cap across all properties — if a
                 single product's state ever exceeds ~960 KB chunked, we'll hit
                 that ceiling. Long-term: prune incident history or move state
                 to Blob (one JSON per product).

  ChangeStore  — public change-request submissions and their approval state.
                 PK="change", RK=<uuid>, status in {pending, approved, rejected}.
"""

from __future__ import annotations

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

# Per-chunk cap in UTF-8 bytes. Azure Table Storage limits a single string
# property to 32K UTF-16 code units (= 64 KiB). 32,000 UTF-8 bytes encodes to
# AT MOST 32,000 UTF-16 code units (ASCII) and fewer for multi-byte text — so
# we sit comfortably under the cap regardless of content.
_CHUNK_BYTES = 32000


def _table_client(table_name: str) -> TableClient:
    account = os.environ["STORAGE_ACCOUNT_NAME"]
    endpoint = f"https://{account}.table.core.windows.net"
    return TableClient(
        endpoint=endpoint,
        table_name=table_name,
        credential=DefaultAzureCredential(),
    )


def _chunk_bytes(text: str, max_bytes: int) -> list[str]:
    """Split `text` into chunks each of UTF-8 size <= `max_bytes`.

    Splits never land mid-character: a chunk re-encoded to UTF-8 is always
    valid UTF-8. Returns at least one element; empty input returns [""].
    """
    if not text:
        return [""]
    data = text.encode("utf-8")
    n = len(data)
    chunks: list[str] = []
    i = 0
    while i < n:
        end = min(i + max_bytes, n)
        # If `end` lands mid-character, walk back to the start of a UTF-8
        # sequence. Continuation bytes are 0b10xxxxxx (0x80..0xBF).
        while end < n and (data[end] & 0xC0) == 0x80:
            end -= 1
        if end <= i:
            # max_bytes is smaller than a single character — degenerate; advance
            # by the full character to make progress.
            end = i + 1
            while end < n and (data[end] & 0xC0) == 0x80:
                end += 1
        chunks.append(data[i:end].decode("utf-8"))
        i = end
    return chunks


def _read_chunks(entity: dict) -> tuple[str | None, bool]:
    """Return (concatenated_text, corrupt).

    Looks at properties named c0, c1, c2, ... (numeric sort, NOT lexical).
    Returns (text, False) for a valid contiguous chunk sequence,
    (None, True) for a gap (e.g., c0 + c2 missing c1),
    (None, False) if no chunk properties exist on the entity.
    """
    chunk_keys = [
        k for k in entity.keys()
        if len(k) > 1 and k[0] == "c" and k[1:].isdigit()
    ]
    if not chunk_keys:
        return None, False
    chunk_keys.sort(key=lambda k: int(k[1:]))
    indices = [int(k[1:]) for k in chunk_keys]
    if indices != list(range(len(chunk_keys))):
        return None, True  # gap
    return "".join(entity[k] for k in chunk_keys), False


def _decode_chunked_or_legacy(entity: dict, what: str) -> dict | list | None:
    """Reconstruct a JSON value from an entity's c0/c1/... chunks, falling back
    to a legacy single "data" property. Returns None for any corruption (gap,
    invalid JSON in chunks, or invalid JSON in legacy data); callers regenerate
    on None. `what` is a label for log messages, e.g. "product nintendo::foo"
    or "seen_updates".
    """
    text, corrupt = _read_chunks(entity)
    if corrupt:
        print(f"  state_store: {what} has chunk gaps; regenerating")
        return None
    if text is not None:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"  state_store: {what} failed JSON decode ({exc}); regenerating")
            return None
    legacy = entity.get("data")
    if legacy:
        try:
            return json.loads(legacy)
        except json.JSONDecodeError as exc:
            print(f"  state_store: {what} legacy 'data' is corrupt ({exc}); regenerating")
            return None
    return None  # caller treats as empty state


class StateStore:
    def __init__(self) -> None:
        self._client = _table_client(STATE_TABLE)

    def read(self) -> dict:
        state: dict = {"_seen_updates": []}
        for entity in self._client.list_entities():
            pk = entity["PartitionKey"]
            rk = entity["RowKey"]
            if pk == SEEN_PK and rk == SEEN_RK:
                value = _decode_chunked_or_legacy(entity, "seen_updates")
                state["_seen_updates"] = list(value) if isinstance(value, list) else []
            elif pk == PRODUCT_PK and "::" in rk:
                team_id, product_id = rk.split("::", 1)
                value = _decode_chunked_or_legacy(
                    entity, f"product {team_id}/{product_id}"
                )
                state.setdefault(team_id, {})[product_id] = (
                    value if isinstance(value, dict) else {}
                )
        return state

    def write(self, state: dict) -> None:
        # seen_updates — chunk across c0/c1/...
        seen = list(state.get("_seen_updates", []))[-MAX_SEEN:]
        seen_chunks = _chunk_bytes(json.dumps(seen), _CHUNK_BYTES)
        seen_entity: dict = {"PartitionKey": SEEN_PK, "RowKey": SEEN_RK}
        for i, chunk in enumerate(seen_chunks):
            seen_entity[f"c{i}"] = chunk
        self._client.upsert_entity(seen_entity, mode=UpdateMode.REPLACE)

        # Per-product state — chunk across c0/c1/... per row.
        # UpdateMode.REPLACE clears any stale "data"/cN properties left over
        # from a previous (larger) write.
        for team_id, products in state.items():
            if team_id == "_seen_updates":
                continue
            for product_id, product_state in products.items():
                payload = json.dumps(product_state, ensure_ascii=False)
                chunks = _chunk_bytes(payload, _CHUNK_BYTES)
                entity: dict = {
                    "PartitionKey": PRODUCT_PK,
                    "RowKey": f"{team_id}::{product_id}",
                }
                for i, chunk in enumerate(chunks):
                    entity[f"c{i}"] = chunk
                self._client.upsert_entity(entity, mode=UpdateMode.REPLACE)


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
