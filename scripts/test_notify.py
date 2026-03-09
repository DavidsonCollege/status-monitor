#!/usr/bin/env python3
"""
Send test notifications to verify Slack and Zoom channel configuration.

Creates a realistic-looking test status event and sends it to all
configured channels, so teams can confirm integrations are working.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from slack_notify import send_slack_notifications
from zoom_notify import send_zoom_notifications

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "teams.json"

TEST_ICON_URL = "https://cdn-icons-png.flaticon.com/512/1827/1827422.png"


def create_test_event(team: dict, base_url: str) -> dict:
    return {
        "id": f"test-{team['id']}",
        "product_id": "test-notification",
        "product_name": "Test Notification",
        "icon_url": TEST_ICON_URL,
        "title": "If you see this, status notifications are working!",
        "link": base_url,
        "summary": (
            "This is a test from Status Monitor. "
            "Real notifications will appear here when vendor status changes are detected. "
            "The sidebar color indicates severity: green=operational, yellow=degraded, "
            "orange=partial outage, red=major outage, blue=maintenance."
        ),
        "date": datetime.now(timezone.utc).isoformat(),
        "status": "degraded",  # Use yellow to show color coding works
        "slack_channel": team.get("slack_channel", ""),
        "zoom_channel": team.get("zoom_channel", ""),
    }


def main():
    print("=" * 60)
    print("  Status Monitor — Test Notification")
    print("=" * 60)

    with open(CONFIG_FILE) as f:
        config = json.load(f)

    teams = config.get("teams", [])
    base_url = os.environ.get(
        "BASE_URL",
        "https://davidsoncollege.github.io/status-monitor/",
    )

    filter_raw = os.environ.get("TEST_TEAMS", "")
    if not filter_raw and len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--teams="):
                filter_raw = arg.split("=", 1)[1]
    team_filter: set[str] = set()
    if filter_raw:
        team_filter = {t.strip().lower() for t in filter_raw.split(",") if t.strip()}
        print(f"  Filter: sending only to team(s): {', '.join(sorted(team_filter))}\n")

    test_items: list[dict] = []
    for team in teams:
        if team_filter and team["id"].lower() not in team_filter:
            continue

        has_slack = bool(team.get("slack_channel"))
        has_zoom = bool(team.get("zoom_channel"))

        if not has_slack and not has_zoom:
            print(f"  ⚠  {team['name']}: no channels configured — skipping")
            continue

        targets = []
        if has_slack:
            targets.append("Slack")
        if has_zoom:
            targets.append("Zoom")

        item = create_test_event(team, base_url)
        test_items.append(item)
        print(f"  ✓  {team['name']}: sending test to {', '.join(targets)}")

    if not test_items:
        print("\nNo teams have notification channels configured. Nothing to send.")
        sys.exit(0)

    print(f"\nSending {len(test_items)} test notification(s)...\n")

    print("--- Slack ---")
    send_slack_notifications(test_items, base_url)

    print("--- Zoom ---")
    send_zoom_notifications(test_items, base_url)

    print("\n" + "=" * 60)
    print("  Done! Check your channels to confirm delivery.")
    print("=" * 60)


if __name__ == "__main__":
    main()
