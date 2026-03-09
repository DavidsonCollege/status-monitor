#!/usr/bin/env python3
"""
Status Monitor â checks vendor status pages for incidents and status changes.

Supports multiple source types:
  - statuspage: Atlassian Statuspage API (majority of vendors)
  - slack_status: Slack's custom status API
  - google_workspace: Google Apps Status Dashboard
  - gcp_status: Google Cloud Platform status (for Gemini/Vertex AI)
  - microsoft_365: Microsoft 365 status (RSS feed)

Each run:
  1. Loads config/teams.json and data/state.json
  2. Fetches current status + active/recent incidents from each vendor
  3. Detects changes (new incidents, status changes, resolutions)
  4. Sends notifications for changes via Slack + Zoom
  5. Writes updated feeds to docs/feeds/ and saves state
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# Allow imports from this directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from slack_notify import send_slack_notifications
from zoom_notify import send_zoom_notifications

# ââ Paths ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config" / "teams.json"
STATE_FILE = BASE_DIR / "data" / "state.json"
FEEDS_DIR = BASE_DIR / "docs" / "feeds"

REQUEST_TIMEOUT = 20

# ââ Status colour mapping ââââââââââââââââââââââââââââââââââââââââââââââââââââââ

# Statuspage API indicator values â our canonical status
STATUSPAGE_INDICATOR_MAP = {
    "none": "operational",
    "minor": "degraded",
    "major": "partial_outage",
    "critical": "major_outage",
    "maintenance": "maintenance",
}

# Statuspage component status values â our canonical status
STATUSPAGE_COMPONENT_MAP = {
    "operational": "operational",
    "degraded_performance": "degraded",
    "partial_outage": "partial_outage",
    "major_outage": "major_outage",
    "under_maintenance": "maintenance",
}

# Incident impact â our canonical status
INCIDENT_IMPACT_MAP = {
    "none": "operational",
    "minor": "degraded",
    "major": "partial_outage",
    "critical": "major_outage",
}

# Canonical status â colour hex
STATUS_COLORS = {
    "operational": "#2fcc66",
    "degraded": "#f1c40f",
    "partial_outage": "#e67e22",
    "major_outage": "#e74c3c",
    "maintenance": "#3498db",
    "unknown": "#95a5a6",
}

# Canonical status â human label
STATUS_LABELS = {
    "operational": "Operational",
    "degraded": "Degraded Performance",
    "partial_outage": "Partial Outage",
    "major_outage": "Major Outage",
    "maintenance": "Under Maintenance",
    "unknown": "Unknown",
}

# Incident status â canonical event type
INCIDENT_STATUS_MAP = {
    "investigating": "investigating",
    "identified": "identified",
    "monitoring": "monitoring",
    "resolved": "resolved",
    "postmortem": "resolved",
    "scheduled": "maintenance_scheduled",
    "in_progress": "maintenance_in_progress",
    "verifying": "monitoring",
    "completed": "resolved",
}


# ââ Helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def generate_event_id(product_id: str, incident_id: str, update_id: str = "") -> str:
    """Generate a stable ID for an incident update event."""
    raw = f"{product_id}:{incident_id}:{update_id}".strip().lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_iso_date(date_str: str) -> str:
    """Parse an ISO date string, return ISO format or empty string."""
    if not date_str:
        return ""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except (ValueError, TypeError):
        return date_str


def status_severity(status: str) -> int:
    """Return numeric severity for sorting (higher = worse)."""
    order = {
        "operational": 0,
        "maintenance": 1,
        "degraded": 2,
        "partial_outage": 3,
        "major_outage": 4,
        "unknown": 5,
    }
    return order.get(status, 5)


# ââ Source handlers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def check_statuspage(product: dict) -> dict:
    """Check an Atlassian Statuspage-based status page.

    Returns: {
        "overall_status": "operational" | "degraded" | ...,
        "components": [{"name": ..., "status": ...}, ...],
        "incidents": [{"id": ..., "name": ..., "status": ..., "impact": ...,
                        "updates": [...], "created_at": ..., "url": ...}, ...]
    }
    """
    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    # Fetch summary (overall status + components)
    try:
        resp = requests.get(f"{api_base}/api/v2/summary.json", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # Overall status
        indicator = data.get("status", {}).get("indicator", "none")
        result["overall_status"] = STATUSPAGE_INDICATOR_MAP.get(indicator, "unknown")

        # Components
        for comp in data.get("components", []):
            # Skip group-level components (they aggregate children)
            if comp.get("group") is True or comp.get("group_id"):
                continue
            comp_status = STATUSPAGE_COMPONENT_MAP.get(comp.get("status", ""), "unknown")
            result["components"].append({
                "name": comp.get("name", ""),
                "status": comp_status,
            })

    except Exception as exc:
        print(f"    [WARN] Failed to fetch summary: {exc}")
        return result

    # Apply region/component filtering
    result["components"] = _filter_components(result["components"], source)
    if result["components"]:
        result["overall_status"] = _recalculate_overall_status(
            result["components"], result["overall_status"]
        )

    # Fetch recent incidents (unresolved + last 50)
    try:
        resp = requests.get(f"{api_base}/api/v2/incidents.json", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        for inc in data.get("incidents", [])[:30]:
            updates = []
            for upd in inc.get("incident_updates", []):
                updates.append({
                    "id": upd.get("id", ""),
                    "status": upd.get("status", ""),
                    "body": upd.get("body", ""),
                    "created_at": parse_iso_date(upd.get("created_at", "")),
                })

            result["incidents"].append({
                "id": inc.get("id", ""),
                "name": inc.get("name", ""),
                "status": inc.get("status", ""),
                "impact": inc.get("impact", "none"),
                "url": inc.get("shortlink", "") or f"{api_base}/incidents/{inc.get('id', '')}",
                "created_at": parse_iso_date(inc.get("created_at", "")),
                "updated_at": parse_iso_date(inc.get("updated_at", "")),
                "resolved_at": parse_iso_date(inc.get("resolved_at", "")),
                "updates": updates,
            })

    except Exception as exc:
        print(f"    [WARN] Failed to fetch incidents: {exc}")

    # Also fetch scheduled maintenances
    try:
        resp = requests.get(f"{api_base}/api/v2/scheduled-maintenances.json", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        for maint in data.get("scheduled_maintenances", [])[:10]:
            updates = []
            for upd in maint.get("incident_updates", []):
                updates.append({
                    "id": upd.get("id", ""),
                    "status": upd.get("status", ""),
                    "body": upd.get("body", ""),
                    "created_at": parse_iso_date(upd.get("created_at", "")),
                })

            result["incidents"].append({
                "id": maint.get("id", ""),
                "name": maint.get("name", ""),
                "status": maint.get("status", ""),
                "impact": "maintenance",
                "url": maint.get("shortlink", "") or f"{api_base}",
                "created_at": parse_iso_date(maint.get("created_at", "")),
                "updated_at": parse_iso_date(maint.get("updated_at", "")),
                "resolved_at": parse_iso_date(maint.get("resolved_at", "")),
                "scheduled_for": parse_iso_date(maint.get("scheduled_for", "")),
                "scheduled_until": parse_iso_date(maint.get("scheduled_until", "")),
                "updates": updates,
                "is_maintenance": True,
            })

    except Exception as exc:
        print(f"    [WARN] Failed to fetch scheduled maintenances: {exc}")

    return result


def check_slack_status(product: dict) -> dict:
    """Check Slack's custom status API."""
    result = {
        "overall_status": "operational",
        "components": [],
        "incidents": [],
    }

    try:
        resp = requests.get("https://slack-status.com/api/v2.0.0/current",
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # Slack uses "active_incidents" array
        status = data.get("status", "ok")
        if status == "ok":
            result["overall_status"] = "operational"
        elif status == "active":
            result["overall_status"] = "degraded"
        else:
            result["overall_status"] = "unknown"

        for inc in data.get("active_incidents", []):
            # Slack incident structure
            updates = []
            for note in inc.get("notes", []):
                updates.append({
                    "id": note.get("id", str(note.get("date_created", ""))),
                    "status": "update",
                    "body": note.get("body", ""),
                    "created_at": note.get("date_created", ""),
                })

            inc_type = inc.get("type", "incident")
            impact = "minor" if inc_type == "notice" else "major"

            result["incidents"].append({
                "id": str(inc.get("id", "")),
                "name": inc.get("title", "Slack Incident"),
                "status": "investigating" if inc.get("status", "") == "active" else inc.get("status", ""),
                "impact": impact,
                "url": f"https://slack-status.com",
                "created_at": inc.get("date_created", ""),
                "updated_at": inc.get("date_updated", ""),
                "resolved_at": "",
                "updates": updates,
            })

            if impact == "major":
                result["overall_status"] = "major_outage"
            elif result["overall_status"] == "operational":
                result["overall_status"] = "degraded"

    except Exception as exc:
        print(f"    [WARN] Failed to fetch Slack status: {exc}")
        result["overall_status"] = "unknown"

    return result


def check_google_workspace(product: dict) -> dict:
    """Check Google Workspace status via the Apps Status Dashboard JSON."""
    result = {
        "overall_status": "operational",
        "components": [],
        "incidents": [],
    }

    try:
        # Google Apps Status Dashboard provides a JSON feed
        resp = requests.get(
            "https://www.google.com/appsstatus/dashboard/incidents.json",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # Check for active incidents (not yet resolved)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)

        for inc in data if isinstance(data, list) else []:
            # Google format: each incident has begin/end times, affected services
            end_time = inc.get("end")
            if end_time:
                try:
                    end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    if end_dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            severity = inc.get("severity", "medium")
            if severity == "high":
                impact = "major"
            elif severity == "medium":
                impact = "minor"
            else:
                impact = "none"

            is_resolved = bool(end_time)
            status = "resolved" if is_resolved else "investigating"

            updates = []
            for upd in inc.get("updates", []):
                updates.append({
                    "id": upd.get("id", str(upd.get("when", ""))),
                    "status": "update",
                    "body": upd.get("message", ""),
                    "created_at": upd.get("when", ""),
                })

            result["incidents"].append({
                "id": str(inc.get("id", "")),
                "name": inc.get("external_desc", inc.get("service_name", "Google Workspace Issue")),
                "status": status,
                "impact": impact,
                "url": f"https://www.google.com/appsstatus/dashboard/",
                "created_at": inc.get("begin", ""),
                "updated_at": inc.get("modified", inc.get("begin", "")),
                "resolved_at": end_time or "",
                "updates": updates,
            })

            if not is_resolved:
                if impact == "major":
                    result["overall_status"] = "major_outage"
                elif result["overall_status"] == "operational":
                    result["overall_status"] = "degraded"

    except Exception as exc:
        print(f"    [WARN] Failed to fetch Google Workspace status: {exc}")
        result["overall_status"] = "unknown"

    return result


def check_gcp_status(product: dict) -> dict:
    """Check Google Cloud Status for AI-related services."""
    result = {
        "overall_status": "operational",
        "components": [],
        "incidents": [],
    }

    service_filter = product.get("source", {}).get("service_filter", [])

    try:
        resp = requests.get(
            "https://status.cloud.google.com/incidents.json",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)

        for inc in data if isinstance(data, list) else []:
            # Filter by service name if specified
            affected = inc.get("service_name", "")
            if service_filter:
                matched = any(f.lower() in affected.lower() for f in service_filter)
                if not matched:
                    # Also check description
                    desc = inc.get("external_desc", "")
                    matched = any(f.lower() in desc.lower() for f in service_filter)
                    if not matched:
                        continue

            end_time = inc.get("end")
            if end_time:
                try:
                    end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                    if end_dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            severity = inc.get("severity", "medium")
            impact = "major" if severity == "high" else "minor"

            is_resolved = bool(end_time)
            status = "resolved" if is_resolved else "investigating"

            updates = []
            for upd in inc.get("updates", []):
                updates.append({
                    "id": str(upd.get("when", "")),
                    "status": "update",
                    "body": upd.get("text", upd.get("message", "")),
                    "created_at": upd.get("when", ""),
                })

            result["incidents"].append({
                "id": str(inc.get("id", inc.get("number", ""))),
                "name": inc.get("external_desc", f"{affected} Issue"),
                "status": status,
                "impact": impact,
                "url": inc.get("uri", "https://status.cloud.google.com"),
                "created_at": inc.get("begin", ""),
                "updated_at": inc.get("modified", ""),
                "resolved_at": end_time or "",
                "updates": updates,
            })

            if not is_resolved:
                if impact == "major":
                    result["overall_status"] = "major_outage"
                elif result["overall_status"] == "operational":
                    result["overall_status"] = "degraded"

    except Exception as exc:
        print(f"    [WARN] Failed to fetch GCP status: {exc}")
        result["overall_status"] = "unknown"

    return result


def _filter_components(components: list[dict], source: dict) -> list[dict]:
    """Filter components by region and/or component name patterns.

    Config options (all optional, in product.source):
        region_filter:    ["US", "United States"] â keep only matching
        component_filter: ["Zoom Meetings", "Phone"] â keep only matching
    If neither filter is set, all components are returned.
    """
    region_filter = source.get("region_filter", [])
    component_filter = source.get("component_filter", [])

    if not region_filter and not component_filter:
        return components

    filtered = []
    for comp in components:
        name = comp.get("name", "")
        name_lower = name.lower()

        if region_filter:
            if not any(r.lower() in name_lower for r in region_filter):
                continue
        if component_filter:
            if not any(f.lower() in name_lower for f in component_filter):
                continue
        filtered.append(comp)

    return filtered


def _recalculate_overall_status(components: list[dict], original_status: str) -> str:
    """Recalculate overall status from filtered components.

    If no components remain after filtering, keep the original API status.
    """
    if not components:
        return original_status

    worst = "operational"
    for comp in components:
        cs = comp.get("status", "operational")
        if status_severity(cs) > status_severity(worst):
            worst = cs
    return worst


def check_statushub(product: dict) -> dict:
    """Check a StatusHub-based status page (e.g., Accruent EMS).

    StatusHub pages expose /api/blocks/statuses/v1 and
    /api/blocks/traffic_lights/v1 endpoints.
    """
    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    # Try traffic_lights endpoint first â it reliably returns aggregate status
    traffic_ok = False
    try:
        resp = requests.get(f"{api_base}/api/blocks/traffic_lights/v1", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        tl = data.get("data", data)
        print(f"      [DEBUG] StatusHub traffic_lights keys: {list(tl.keys()) if isinstance(tl, dict) else type(tl)}, all_up={tl.get('all_up') if isinstance(tl, dict) else 'N/A'}")
        if isinstance(tl, dict) and "all_up" in tl:
            traffic_ok = True
            if tl.get("all_up") in (True, "true", "True", 1, "1"):
                result["overall_status"] = "operational"
            elif tl.get("count_status_3", 0) > 0:
                result["overall_status"] = "major_outage"
            elif tl.get("count_status_2", 0) > 0:
                result["overall_status"] = "degraded"
            else:
                result["overall_status"] = "operational"
    except Exception as exc:
        print(f"    [WARN] StatusHub traffic_lights failed: {exc}")

    # Also try statuses endpoint for component-level detail
    try:
        resp = requests.get(f"{api_base}/api/blocks/statuses/v1", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # StatusHub may return groups with nested services or a flat list
        raw_services = []
        if isinstance(data, list):
            raw_services = data
        elif isinstance(data, dict):
            groups = data.get("data", data.get("groups", []))
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict):
                        svcs = group.get("services", group.get("items", []))
                        if svcs:
                            raw_services.extend(svcs)
                        else:
                            raw_services.append(group)
            else:
                raw_services = data.get("services", [])

        worst = "operational"
        for svc in raw_services:
            name = svc.get("name", svc.get("title", "Unknown"))
            raw_status = svc.get("status", svc.get("current_status", "")).lower()

            if any(k in raw_status for k in ("operational", "up", "ok", "1")):
                cs = "operational"
            elif any(k in raw_status for k in ("degraded", "slow", "warning", "2")):
                cs = "degraded"
            elif any(k in raw_status for k in ("down", "outage", "major", "critical", "3")):
                cs = "major_outage"
            elif "maintenance" in raw_status:
                cs = "maintenance"
            else:
                cs = "unknown"

            result["components"].append({"name": name, "status": cs})
            if status_severity(cs) > status_severity(worst):
                worst = cs

        # Only override traffic_lights result if we got meaningful component data
        if result["components"] and not traffic_ok:
            result["overall_status"] = worst

    except Exception as exc:
        print(f"    [WARN] StatusHub statuses failed: {exc}")

    # Apply region/component filtering
    result["components"] = _filter_components(result["components"], source)
    if result["components"] and not traffic_ok:
        result["overall_status"] = _recalculate_overall_status(
            result["components"], result["overall_status"]
        )

    # Fallback: if still unknown, try HTML scraping the main page
    if result["overall_status"] == "unknown":
        print(f"      [DEBUG] StatusHub API returned unknown, trying HTML fallback")
        try:
            fallback_resp = requests.get(api_base, timeout=REQUEST_TIMEOUT)
            fallback_resp.raise_for_status()
            fallback_html = fallback_resp.text.lower()
            if any(p in fallback_html for p in ["all systems operational", "all systems are operational", "all services are online"]):
                result["overall_status"] = "operational"
            elif any(p in fallback_html for p in ["major outage", "service disruption"]):
                result["overall_status"] = "major_outage"
            elif any(p in fallback_html for p in ["degraded performance", "minor issue"]):
                result["overall_status"] = "degraded"
            elif "operational" in fallback_html:
                result["overall_status"] = "operational"
        except Exception as exc:
            print(f"    [WARN] StatusHub HTML fallback failed: {exc}")

    return result



def check_uptimerobot(product: dict) -> dict:
    """Check an UptimeRobot-based status page (e.g., Scribe).

    UptimeRobot public status pages expose /api/getMonitorList/{api_key}.
    Status codes: 0=paused, 1=not checked yet, 2=up, 8=seems down, 9=down.
    """
    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")
    api_key = source.get("api_key", "")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    if not api_key:
        # Try to extract from known URL pattern
        # UptimeRobot status pages: /api/getMonitorList/{key}
        print("    [WARN] No api_key configured for UptimeRobot")
        return result

    try:
        resp = requests.get(
            f"{api_base}/api/getMonitorList/{api_key}",
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # UptimeRobot public status page API may use "status" (not "stat")
        stat = data.get("stat", data.get("status", ""))
        if stat != "ok":
            print(f"    [WARN] UptimeRobot stat={stat}")
            return result

        psp = data.get("psp", {})
        monitors = psp.get("monitors", [])

        worst = "operational"
        for mon in monitors:
            name = mon.get("name", "Unknown")
            status_code = mon.get("status", None)
            status_class = mon.get("statusClass", "")

            # Map UptimeRobot status â public pages use statusClass strings,
            # API v2 uses numeric status codes
            if status_class:
                if status_class in ("success", "green"):
                    cs = "operational"
                elif status_class in ("warning", "yellow"):
                    cs = "degraded"
                elif status_class in ("danger", "red"):
                    cs = "major_outage"
                else:
                    cs = "unknown"
            elif status_code is not None:
                if status_code == 2:
                    cs = "operational"
                elif status_code == 8:
                    cs = "degraded"
                elif status_code == 9:
                    cs = "major_outage"
                elif status_code == 0:
                    cs = "maintenance"
                else:
                    cs = "unknown"
            else:
                cs = "unknown"

            result["components"].append({"name": name, "status": cs})
            if status_severity(cs) > status_severity(worst):
                worst = cs

        if monitors:
            result["overall_status"] = worst

        # Check for incidents in the event feed
        try:
            resp2 = requests.get(
                f"{api_base}/api/getEventFeed/{api_key}",
                timeout=REQUEST_TIMEOUT,
            )
            resp2.raise_for_status()
            feed_data = resp2.json()

            events = feed_data.get("psp", {}).get("events", [])
            for evt in events[:10]:
                evt_status = "resolved" if evt.get("type", 0) == 2 else "investigating"
                result["incidents"].append({
                    "id": str(evt.get("id", "")),
                    "name": evt.get("text", "Incident"),
                    "status": evt_status,
                    "impact": "minor",
                    "url": api_base,
                    "created_at": evt.get("datetime", ""),
                    "updated_at": evt.get("datetime", ""),
                    "resolved_at": evt.get("datetime", "") if evt_status == "resolved" else "",
                    "updates": [],
                })
        except Exception:
            pass  # Event feed is optional

    except Exception as exc:
        print(f"    [WARN] UptimeRobot fetch failed: {exc}")

    # Apply filters
    result["components"] = _filter_components(result["components"], source)
    if result["components"]:
        result["overall_status"] = _recalculate_overall_status(
            result["components"], result["overall_status"]
        )

    return result


def check_cstate(product: dict) -> dict:
    """Check a cState-based status page (e.g., Short.io).

    cState is a Hugo-based static status page generator.
    It exposes /index.json with system status data.
    """
    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    try:
        resp = requests.get(f"{api_base}/index.json", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # cState index.json structure varies by version
        # Common patterns: {"is": "ok/disrupted/down", "systems": [...]}
        # or array of categories with systems
        overall = data.get("is", data.get("status", ""))
        summary_status = data.get("summaryStatus", "")

        # Prefer summaryStatus (cState v6+) over "is" (which may be "index")
        effective_status = summary_status if summary_status else overall

        if effective_status in ("ok", "operational"):
            result["overall_status"] = "operational"
        elif effective_status in ("disrupted", "degraded"):
            result["overall_status"] = "degraded"
        elif effective_status in ("down", "outage"):
            result["overall_status"] = "major_outage"

        # Extract systems/categories
        systems = data.get("systems", data.get("categories", []))
        if isinstance(systems, list):
            for item in systems:
                if isinstance(item, dict):
                    # Could be a category with sub-systems
                    if "systems" in item:
                        for sys_item in item["systems"]:
                            name = sys_item.get("name", "")
                            s = sys_item.get("status", "ok")
                            cs = "operational" if s in ("ok", "operational") else (
                                "degraded" if s in ("disrupted", "degraded") else (
                                    "major_outage" if s in ("down",) else "unknown"
                                )
                            )
                            result["components"].append({"name": name, "status": cs})
                    else:
                        name = item.get("name", item.get("title", ""))
                        s = item.get("status", "ok")
                        cs = "operational" if s in ("ok", "operational") else (
                            "degraded" if s in ("disrupted", "degraded") else (
                                "major_outage" if s in ("down",) else "unknown"
                            )
                        )
                        if name:
                            result["components"].append({"name": name, "status": cs})

    except Exception as exc:
        print(f"    [WARN] cState fetch failed: {exc}")

    # Apply filters
    result["components"] = _filter_components(result["components"], source)
    if result["components"]:
        result["overall_status"] = _recalculate_overall_status(
            result["components"], result["overall_status"]
        )

    return result


def check_sorry(product: dict) -> dict:
    """Check a Sorryâ¢-based status page (e.g., SecureW2).

    Sorryâ¢ pages may expose a JSON API. We try common patterns.
    Falls back to parsing the HTML page for status indicators.
    """
    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    # Try JSON API first (Accept: application/json)
    try:
        resp = requests.get(
            api_base,
            headers={"Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            data = resp.json()
            # Parse Sorryâ¢ JSON format
            page = data.get("page", data)
            status = page.get("status", "")
            if status:
                if status in ("operational", "up", "green"):
                    result["overall_status"] = "operational"
                elif status in ("degraded", "yellow", "warning"):
                    result["overall_status"] = "degraded"
                elif status in ("down", "red", "critical"):
                    result["overall_status"] = "major_outage"

            components = page.get("components", page.get("services", []))
            for comp in components if isinstance(components, list) else []:
                name = comp.get("name", "")
                s = comp.get("status", "").lower()
                cs = "operational" if s in ("operational", "up") else (
                    "degraded" if s in ("degraded", "slow") else (
                        "major_outage" if s in ("down", "outage") else "unknown"
                    )
                )
                if name:
                    result["components"].append({"name": name, "status": cs})
            return result
    except Exception:
        pass

    # Fallback: parse HTML page
    try:
        resp = requests.get(api_base, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text.lower()

        if any(p in html for p in [
                "all systems operational",
                "all systems are operational",
                "all systems are go",
                "all services operational",
            ]):
            result["overall_status"] = "operational"
        elif "experiencing issues" in html or "degraded" in html:
            result["overall_status"] = "degraded"
        elif "major outage" in html or "service disruption" in html:
            result["overall_status"] = "major_outage"
        elif "maintenance" in html:
            result["overall_status"] = "maintenance"
        else:
            # Look for common status page patterns
            if "operational" in html:
                result["overall_status"] = "operational"

    except Exception as exc:
        print(f"    [WARN] Sorryâ¢ fetch failed: {exc}")

    return result


def check_html_scrape(product: dict) -> dict:
    """Generic HTML scraper for status pages without structured APIs.

    Fetches the page HTML and looks for common status indicators.
    Used as a fallback for platforms like Spike.sh and ServiceNow.
    """
    source = product.get("source", {})
    api_base = source.get("api_base", source.get("status_url", "")).rstrip("/")

    result = {
        "overall_status": "unknown",
        "components": [],
        "incidents": [],
    }

    if not api_base:
        return result

    try:
        resp = requests.get(api_base, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text.lower()

        # Check for common status page patterns
        if any(p in html for p in [
            "all systems operational",
            "all systems are operational",
            "all services are online",
            "all components operational",
            "everything is running smoothly",
            "no system is reporting an issue",
            "no issues reported",
            "all systems are go",
        ]):
            result["overall_status"] = "operational"
        elif any(p in html for p in [
            "major outage",
            "service disruption",
            "system down",
            "critical",
        ]):
            result["overall_status"] = "major_outage"
        elif any(p in html for p in [
            "partial outage",
            "partially degraded",
            "some systems",
        ]):
            result["overall_status"] = "partial_outage"
        elif any(p in html for p in [
            "degraded performance",
            "experiencing issues",
            "elevated error",
            "minor issue",
        ]):
            result["overall_status"] = "degraded"
        elif any(p in html for p in [
            "under maintenance",
            "scheduled maintenance",
            "planned maintenance",
        ]):
            result["overall_status"] = "maintenance"
        elif "operational" in html:
            # Generic fallback â if the word "operational" appears
            result["overall_status"] = "operational"

    except Exception as exc:
        print(f"    [WARN] HTML scrape failed: {exc}")

    return result


def check_exlibris(product: dict) -> dict:
    """Check ExLibris/Clarivate status (ServiceNow portal).

    Two-step: GET page for CSRF token, then POST to widget API.
    """
    import re as _re

    source = product.get("source", {})
    api_base = source.get("api_base", "").rstrip("/")
    service_filter = [f.lower() for f in source.get("service_filter", [])]
    widget_id = source.get("widget_id", "e40b1d3d3b00b25cd6fc6e8aa4e45a9f")

    result = {"overall_status": "unknown", "components": [], "incidents": []}
    if not api_base:
        return result

    try:
        session = requests.Session()
        page = session.get(api_base, timeout=REQUEST_TIMEOUT)
        page.raise_for_status()

        m = _re.search(r"g_ck\s*=\s*['\"](\w+)['\"]", page.text)
        if not m:
            # No CSRF token — page may have changed structure or be blocked.
            # Fall back to HTTP health-check.
            if page.status_code == 200:
                result["overall_status"] = "operational"
                print("    [INFO] ExLibris: g_ck not found, HTTP 200 health-check passed")
            else:
                print("    [WARN] ExLibris: g_ck token not found")
            return result

        api_url = f"{api_base}/api/now/sp/rectangle/{widget_id}"
        r = session.post(api_url, json={}, headers={
            "Content-Type": "application/json",
            "X-UserToken": m.group(1),
        }, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        ct = r.headers.get("content-type", "")
        print(f"      [DEBUG] ExLibris API response: status={r.status_code}, content-type={ct}, body[:200]={r.text[:200]}")
        if "json" not in ct and "javascript" not in ct:
            # Widget API returned HTML (likely IP-blocked). Fall back to
            # treating the page itself as a health-check: if the status
            # page loads (HTTP 200) and the initial HTML contains the
            # "no system is reporting an issue" banner OR the page simply
            # loaded successfully, assume operational.  ExLibris shows a
            # prominent banner when any service has issues.
            html_lower = page.text.lower()
            if "no system is reporting an issue" in html_lower:
                result["overall_status"] = "operational"
                print("    [INFO] ExLibris: widget API blocked, but page banner says all OK")
            elif page.status_code == 200:
                result["overall_status"] = "operational"
                print("    [INFO] ExLibris: widget API blocked, HTTP 200 health-check passed")
            else:
                print("    [WARN] ExLibris: API returned non-JSON and page unhealthy")
            return result

        services = r.json().get("result", {}).get("data", {}).get("services", [])
        if not services:
            print("    [WARN] ExLibris: no services in response")
            return result

        if service_filter:
            services = [s for s in services
                        if any(f in s.get("name", "").lower() or f in s.get("cat", "").lower()
                               for f in service_filter)]

        worst = "operational"
        for svc in services:
            name = svc.get("name", "Unknown")
            cur = svc.get("curOutages", [])
            val = cur[0] if cur else 0
            cs_map = {0: "operational", 1: "major_outage", 2: "degraded", 3: "maintenance"}
            status = cs_map.get(val, "unknown")
            result["components"].append({"name": name, "status": status})
            if status_severity(status) > status_severity(worst):
                worst = status

        if result["components"]:
            result["overall_status"] = worst

    except Exception as exc:
        print(f"    [WARN] ExLibris check failed: {exc}")

    return result


def check_microsoft_365(product: dict) -> dict:
    """Check Microsoft 365 status via the Azure status RSS feed."""
    result = {
        "overall_status": "operational",
        "components": [],
        "incidents": [],
    }

    try:
        import feedparser
        feed = feedparser.parse("https://azure.status.microsoft/en-us/status/feed/")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=7)

        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "https://status.cloud.microsoft/m365")
            published = entry.get("published", "")

            # Filter for M365-related entries
            m365_keywords = ["microsoft 365", "office 365", "outlook", "teams",
                             "sharepoint", "onedrive", "exchange", "azure ad",
                             "entra", "intune"]
            text_lower = (title + " " + summary).lower()
            if not any(kw in text_lower for kw in m365_keywords):
                continue

            # Determine if resolved
            is_resolved = "resolved" in text_lower or "mitigated" in text_lower

            result["incidents"].append({
                "id": hashlib.sha256(title.encode()).hexdigest()[:16],
                "name": title,
                "status": "resolved" if is_resolved else "investigating",
                "impact": "minor",
                "url": link,
                "created_at": published,
                "updated_at": published,
                "resolved_at": published if is_resolved else "",
                "updates": [{
                    "id": hashlib.sha256(summary.encode()).hexdigest()[:12],
                    "status": "update",
                    "body": summary[:500],
                    "created_at": published,
                }],
            })

            if not is_resolved:
                result["overall_status"] = "degraded"

    except Exception as exc:
        print(f"    [WARN] Failed to fetch Microsoft 365 status: {exc}")
        result["overall_status"] = "unknown"

    return result


# ââ Source dispatcher ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

SOURCE_HANDLERS = {
    "statuspage": check_statuspage,
    "slack_status": check_slack_status,
    "google_workspace": check_google_workspace,
    "gcp_status": check_gcp_status,
    "microsoft_365": check_microsoft_365,
    "statushub": check_statushub,
    "uptimerobot": check_uptimerobot,
    "cstate": check_cstate,
    "sorry": check_sorry,
    "html_scrape": check_html_scrape,
    "exlibris": check_exlibris,
}


def check_product(product: dict) -> dict:
    """Check status for a single product using the appropriate handler."""
    source = product.get("source", {})
    source_type = source.get("type", "")
    handler = SOURCE_HANDLERS.get(source_type)

    if not handler:
        print(f"    [WARN] Unknown source type: {source_type}")
        return {"overall_status": "unknown", "components": [], "incidents": []}

    return handler(product)


# ââ Change detection âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def detect_changes(product_id: str, product_name: str, current: dict,
                   previous_state: dict, seen_updates: set) -> list[dict]:
    """Compare current status with previous state to detect changes.

    Returns a list of notification events (new incidents, status changes,
    resolutions).
    """
    events = []
    prev_status = previous_state.get("overall_status", "unknown")
    curr_status = current.get("overall_status", "unknown")

    # Overall status change
    if prev_status != curr_status and prev_status != "unknown":
        event_id = generate_event_id(product_id, "status_change",
                                     f"{curr_status}_{datetime.now(timezone.utc).isoformat()}")
        events.append({
            "type": "status_change",
            "product_id": product_id,
            "product_name": product_name,
            "status": curr_status,
            "previous_status": prev_status,
            "title": f"{product_name}: {STATUS_LABELS.get(curr_status, curr_status)}",
            "summary": f"Status changed from {STATUS_LABELS.get(prev_status, prev_status)} to {STATUS_LABELS.get(curr_status, curr_status)}",
            "link": "",  # Will be enriched with status_url in main loop
            "date": datetime.now(timezone.utc).isoformat(),
            "id": event_id,
        })

    # Incident-level changes
    prev_incidents = {inc["id"]: inc for inc in previous_state.get("incidents", [])}

    for inc in current.get("incidents", []):
        inc_id = inc.get("id", "")
        inc_name = inc.get("name", "Unknown Incident")
        inc_status = inc.get("status", "")
        inc_url = inc.get("url", "")
        inc_impact = inc.get("impact", "none")

        if inc_id not in prev_incidents:
            # New incident
            event_id = generate_event_id(product_id, inc_id, "new")
            if event_id not in seen_updates:
                canonical_status = INCIDENT_STATUS_MAP.get(inc_status, inc_status)
                is_maint = inc.get("is_maintenance", False) or inc_impact == "maintenance"
                type_label = "Scheduled Maintenance" if is_maint else "Incident"
                events.append({
                    "type": "new_incident",
                    "product_id": product_id,
                    "product_name": product_name,
                    "incident_id": inc_id,
                    "status": canonical_status if canonical_status != "investigating" else (
                        "maintenance" if is_maint else INCIDENT_IMPACT_MAP.get(inc_impact, "degraded")
                    ),
                    "title": f"{product_name}: {inc_name}",
                    "summary": _latest_update_body(inc),
                    "link": inc_url,
                    "date": inc.get("created_at", "") or datetime.now(timezone.utc).isoformat(),
                    "id": event_id,
                    "incident_status": inc_status,
                    "is_maintenance": is_maint,
                })
        else:
            # Existing incident â check for new updates
            prev_inc = prev_incidents[inc_id]
            prev_update_ids = {u.get("id", "") for u in prev_inc.get("updates", []) if u.get("id")}

            for upd in inc.get("updates", []):
                upd_id = upd.get("id", "")
                if upd_id and upd_id not in prev_update_ids:
                    event_id = generate_event_id(product_id, inc_id, upd_id)
                    if event_id not in seen_updates:
                        canonical_status = INCIDENT_STATUS_MAP.get(
                            upd.get("status", inc_status), inc_status
                        )
                        # For resolution events, always mark as operational
                        if canonical_status == "resolved":
                            color_status = "operational"
                        elif canonical_status == "monitoring":
                            color_status = "degraded"
                        else:
                            color_status = INCIDENT_IMPACT_MAP.get(inc_impact, "degraded")

                        events.append({
                            "type": "incident_update",
                            "product_id": product_id,
                            "product_name": product_name,
                            "incident_id": inc_id,
                            "status": color_status,
                            "title": f"{product_name}: {inc_name} â {STATUS_LABELS.get(canonical_status, canonical_status)}",
                            "summary": upd.get("body", "")[:500] or f"Status: {canonical_status}",
                            "link": inc_url,
                            "date": upd.get("created_at", "") or datetime.now(timezone.utc).isoformat(),
                            "id": event_id,
                            "incident_status": canonical_status,
                        })

            # Check if status changed (e.g., investigating â resolved)
            if prev_inc.get("status") != inc_status:
                canonical_status = INCIDENT_STATUS_MAP.get(inc_status, inc_status)
                event_id = generate_event_id(product_id, inc_id, f"status_{inc_status}")
                if event_id not in seen_updates and canonical_status == "resolved":
                    events.append({
                        "type": "incident_resolved",
                        "product_id": product_id,
                        "product_name": product_name,
                        "incident_id": inc_id,
                        "status": "operational",
                        "title": f"{product_name}: {inc_name} â Resolved",
                        "summary": _latest_update_body(inc) or "This incident has been resolved.",
                        "link": inc_url,
                        "date": inc.get("resolved_at", "") or datetime.now(timezone.utc).isoformat(),
                        "id": event_id,
                        "incident_status": "resolved",
                    })

    return events


def _latest_update_body(incident: dict) -> str:
    """Get the body text of the most recent update in an incident."""
    updates = incident.get("updates", [])
    if updates:
        return updates[0].get("body", "")[:500]
    return ""


# ââ Feed generation ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def build_feed(team_id: str, events: list[dict], existing_feed: list[dict]) -> list[dict]:
    """Build updated feed JSON for a team, merging new events with history."""
    existing_ids = {item["id"] for item in existing_feed}
    new_items = []

    for evt in events:
        if evt["id"] in existing_ids:
            continue
        new_items.append({
            "id": evt["id"],
            "product_id": evt["product_id"],
            "product_name": evt["product_name"],
            "icon_url": "",  # Will be filled from config
            "title": evt["title"],
            "link": evt.get("link", ""),
            "summary": evt.get("summary", ""),
            "date": evt.get("date", datetime.now(timezone.utc).isoformat()),
            "status": evt.get("status", "unknown"),
            "incident_status": evt.get("incident_status", ""),
            "type": evt.get("type", ""),
        })

    # Merge: new items first, then existing (capped at 200)
    feed = new_items + existing_feed
    return feed[:200]


def build_status_summary(team: dict, product_statuses: dict) -> dict:
    """Build a status summary object for the dashboard."""
    summary = {
        "team_id": team["id"],
        "team_name": team["name"],
        "last_checked": datetime.now(timezone.utc).isoformat(),
        "products": [],
    }

    for product in team.get("products", []):
        pid = product["id"]
        status_data = product_statuses.get(pid, {})
        overall = status_data.get("overall_status", "unknown")

        active_incidents = [
            inc for inc in status_data.get("incidents", [])
            if inc.get("status") not in ("resolved", "postmortem", "completed")
        ]

        summary["products"].append({
            "id": pid,
            "name": product["name"],
            "icon_url": product.get("icon_url", ""),
            "status_url": product.get("status_url", ""),
            "overall_status": overall,
            "active_incidents": len(active_incidents),
            "components": status_data.get("components", []),
        })

    return summary


# ââ Main âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    print("=" * 70)
    print("  Status Monitor")
    print(f"  Run time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    print()

    # Load configuration
    with open(CONFIG_FILE) as f:
        config = json.load(f)

    teams = config.get("teams", [])

    # Load previous state
    state: dict = {}
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)

    # Load seen updates to prevent duplicate notifications
    seen_updates: set = set(state.get("_seen_updates", []))

    base_url = os.environ.get(
        "BASE_URL",
        "https://davidsoncollege.github.io/status-monitor/",
    )

    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    all_new_events: list[dict] = []
    new_state: dict = {"_seen_updates": []}
    total_new = 0

    for team in teams:
        team_id = team["id"]
        team_name = team["name"]
        products = team.get("products", [])

        print(f"--- Team: {team_name} ({len(products)} products) ---")
        print()

        team_events: list[dict] = []
        product_statuses: dict = {}
        team_state = state.get(team_id, {})

        for product in products:
            pid = product["id"]
            pname = product["name"]
            print(f"  Checking: {pname}")

            try:
                status_data = check_product(product)
            except Exception as exc:
                print(f"    [ERROR] {exc}")
                status_data = {"overall_status": "unknown", "components": [], "incidents": []}

            product_statuses[pid] = status_data

            # Get previous state for this product
            prev_product_state = team_state.get(pid, {})

            # Detect changes
            events = detect_changes(pid, pname, status_data, prev_product_state, seen_updates)

            if events:
                for evt in events:
                    # Enrich with team routing info
                    evt["slack_channel"] = team.get("slack_channel", "")
                    evt["zoom_channel"] = team.get("zoom_channel", "")
                    evt["icon_url"] = product.get("icon_url", "")
                    if not evt.get("link"):
                        evt["link"] = product.get("status_url", "")
                    seen_updates.add(evt["id"])
                print(f"    {len(events)} change(s) detected")
                team_events.extend(events)
            else:
                overall = status_data.get("overall_status", "unknown")
                active = sum(1 for i in status_data.get("incidents", [])
                             if i.get("status") not in ("resolved", "postmortem", "completed"))
                print(f"    Status: {STATUS_LABELS.get(overall, overall)}"
                      + (f" ({active} active incident(s))" if active else ""))

            # Save product state for next run
            if team_id not in new_state:
                new_state[team_id] = {}
            new_state[team_id][pid] = {
                "overall_status": status_data.get("overall_status", "unknown"),
                "incidents": status_data.get("incidents", []),
                "components": status_data.get("components", []),
            }

            time.sleep(0.5)  # Be polite to APIs

        # Load existing feed
        feed_file = FEEDS_DIR / f"{team_id}.json"
        existing_feed: list[dict] = []
        if feed_file.exists():
            with open(feed_file) as f:
                existing_feed = json.load(f)

        # Build updated feed
        feed = build_feed(team_id, team_events, existing_feed)

        # Enrich feed items with icons from config
        product_icons = {p["id"]: p.get("icon_url", "") for p in products}
        for item in feed:
            if not item.get("icon_url"):
                item["icon_url"] = product_icons.get(item.get("product_id", ""), "")

        # Write feed
        with open(feed_file, "w") as f:
            json.dump(feed, f, indent=2)

        # Write status summary for dashboard
        summary = build_status_summary(team, product_statuses)
        summary_file = FEEDS_DIR / f"{team_id}-status.json"
        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)

        new_count = len(team_events)
        total_new += new_count
        all_new_events.extend(team_events)

        print()
        print(f"  Team '{team_name}': {new_count} new event(s), {len(feed)} total in feed")
        print()

    # Save state
    # Keep only last 5000 seen update IDs to prevent unbounded growth
    new_state["_seen_updates"] = list(seen_updates)[-5000:]
    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)

    # Send notifications
    if all_new_events:
        print(f"--- Sending notifications for {len(all_new_events)} event(s) ---")
        print()

        print("--- Slack ---")
        send_slack_notifications(all_new_events, base_url)
        print()

        print("--- Zoom ---")
        send_zoom_notifications(all_new_events, base_url)
        print()

    print("=" * 70)
    print(f"  Done! {total_new} new event(s) found across all teams.")
    print(f"  Feeds written to: {FEEDS_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()
