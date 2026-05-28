#!/usr/bin/env python3
"""Seed the teamsConfig table from config/teams.json (single row, idempotent).

Loads the repo's config/teams.json and upserts it as one entity
(PartitionKey="config", RowKey="current") with the JSON serialized into a "data"
column — exactly the shape app/config_store.py reads. Authenticates with
DefaultAzureCredential, so the caller needs a Storage Table data role (e.g.
"Storage Table Data Contributor") on the storage account.

Usage:
    python scripts/seed_teamsconfig.py
Env:
    STORAGE_ACCOUNT_NAME   storage account name (default: dcstatusmonitor)
"""

import json
import os
from pathlib import Path

from azure.data.tables import TableClient, UpdateMode
from azure.identity import DefaultAzureCredential

STORAGE_ACCOUNT = os.environ.get("STORAGE_ACCOUNT_NAME", "dcstatusmonitor")
TABLE_NAME = "teamsConfig"
PARTITION_KEY = "config"
ROW_KEY = "current"


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config" / "teams.json"
    config = json.loads(config_path.read_text())

    endpoint = "https://{}.table.core.windows.net".format(STORAGE_ACCOUNT)
    client = TableClient(
        endpoint=endpoint,
        table_name=TABLE_NAME,
        credential=DefaultAzureCredential(),
    )

    data = json.dumps(config, ensure_ascii=False)
    client.upsert_entity(
        {"PartitionKey": PARTITION_KEY, "RowKey": ROW_KEY, "data": data},
        mode=UpdateMode.REPLACE,
    )
    print("Seeded {}/{} in table '{}' ({} bytes) from {}".format(
        PARTITION_KEY, ROW_KEY, TABLE_NAME, len(data), config_path))

    # Verify: read the row back and confirm the JSON parses.
    row = client.get_entity(PARTITION_KEY, ROW_KEY)
    parsed = json.loads(row["data"])
    teams = parsed.get("teams", [])
    print("Verified read-back: JSON parses, {} team(s).".format(len(teams)))


if __name__ == "__main__":
    main()
