#!/usr/bin/env python3
"""Sprint pulse analyzer: runs deterministic alert rules on fetched sprint data."""

import json
import sys
from datetime import datetime, timedelta, timezone


def parse_dt(dt_str):
    if not dt_str:
        return None
    dt_str = str(dt_str)
    # Strip sub-second precision (e.g. ".886") before timezone parsing
    # Jira format: 2026-03-26T08:51:55.886+1030
    import re
    dt_str = re.sub(r"\.\d+", "", dt_str)
    dt_str = dt_str.replace("Z", "+00:00")
    # Insert colon in timezone offset if missing (e.g. +1030 → +10:30)
    m = re.match(r"(.*[+-])(\d{2})(\d{2})$", dt_str)
    if m:
        dt_str = "%s%s:%s" % (m.group(1), m.group(2), m.group(3))
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def previous_business_day(dt):
    """Return the date of the most recent business day before dt.

    On Monday, returns Friday. On Tuesday-Friday, returns the previous day.
    On weekends (shouldn't happen in practice), returns Friday.
    """
    d = dt.date() if hasattr(dt, 'date') else dt
    if d.weekday() == 0:  # Monday
        offset = 3
    elif d.weekday() == 6:  # Sunday
        offset = 2
    else:
        offset = 1
    return d - timedelta(days=offset)


def business_days_since(dt, now):
    """Calculate approximate business days between two datetimes."""
    if not dt or not now:
        return None
    total_seconds = (now - dt).total_seconds()
    calendar_days = total_seconds / 86400
    # Rough business day approximation: 5/7 of calendar days
    return calendar_days * 5.0 / 7.0


def find_last_activity(issue):
    """Find the most recent activity timestamp across all sources for an issue.

    Sources checked:
    - Jira changelog entries
    - Jira comments
    - GitLab MR updated_at
    - GitLab MR notes
    """
    timestamps = []

    # Changelog entries
    for entry in issue.get("changelog", []):
        dt = parse_dt(entry.get("created"))
        if dt:
            timestamps.append(("changelog", dt, entry.get("author", "")))

    # Jira comments
    for comment in issue.get("comments", []):
        dt = parse_dt(comment.get("created"))
        if dt:
            timestamps.append(("comment", dt, comment.get("author", "")))

    # MR activity
    for mr in issue.get("merge_requests", []):
        dt = parse_dt(mr.get("updated_at"))
        if dt:
            timestamps.append(("mr_update", dt, mr.get("author", "")))
        for note in mr.get("notes", []):
            dt = parse_dt(note.get("created_at"))
            if dt:
                timestamps.append(("mr_note", dt, note.get("author", "") or note.get("author_name", "")))

    if not timestamps:
        return None, None, None

    timestamps.sort(key=lambda x: x[1], reverse=True)
    source, dt, author = timestamps[0]
    return dt, source, author


def find_status_entry_time(issue):
    """Find when the issue entered its current status from the changelog."""
    current_status = issue.get("status", "")
    changelog = issue.get("changelog", [])

    # Walk changelog in reverse to find the most recent transition INTO current status
    for entry in reversed(changelog):
        for item in entry.get("items", []):
            if item.get("field") == "status" and item.get("to_string") == current_status:
                return parse_dt(entry.get("created"))

    return None


def analyze_stale_items(issues, now, stale_threshold_days=1.0):
    """Detect items in active statuses with no recent activity."""
    alerts = []

    for issue in issues:
        if not issue.get("is_active"):
            continue

        last_activity_dt, source, author = find_last_activity(issue)
        status_entry_dt = find_status_entry_time(issue)

        # Use last activity if available, otherwise fall back to status entry time
        reference_dt = last_activity_dt or status_entry_dt

        if not reference_dt:
            # No activity data at all — flag it
            alerts.append({
                "key": issue["key"],
                "summary": issue["summary"],
                "assignee": issue.get("assignee", "Unassigned"),
                "column": issue.get("column", ""),
                "story_points": issue.get("story_points"),
                "days_stale": None,
                "last_activity": None,
                "last_activity_source": None,
                "reason": "No activity data available",
            })
            continue

        days_since = business_days_since(reference_dt, now)
        if days_since is not None and days_since > stale_threshold_days:
            alerts.append({
                "key": issue["key"],
                "summary": issue["summary"],
                "assignee": issue.get("assignee", "Unassigned"),
                "column": issue.get("column", ""),
                "story_points": issue.get("story_points"),
                "days_stale": round(days_since, 1),
                "last_activity": reference_dt.isoformat(),
                "last_activity_source": source,
                "reason": "No activity for %.1f business days (threshold: %.1f)" % (days_since, stale_threshold_days),
            })

    return alerts



def build_status_sets(support_board_config):
    """Extract status ID sets for key columns from support board config."""
    todo_ids = set()
    closed_ids = set()
    awaiting_ids = set()
    if not support_board_config:
        return todo_ids, closed_ids, awaiting_ids
    for col in support_board_config:
        name = col.get("name", "").lower().strip()
        if name == "to do":
            todo_ids = set(col.get("statuses", []))
        elif name in ("closed", "done", "resolved", "completed"):
            closed_ids = set(col.get("statuses", []))
        elif "awaiting" in name or "waiting" in name:
            awaiting_ids = set(col.get("statuses", []))
    return todo_ids, closed_ids, awaiting_ids


def analyze_support_tickets(support_tickets, now, sprint_start, support_board_config=None):
    """Analyze support tickets for alerts: new in to-do, unacknowledged >24h, SLA risk.

    Input is all team-labelled tickets across all columns.
    - New / Unacknowledged alerts: only for tickets in the 'To do' column.
    - SLA risk: any non-closed ticket approaching its resolution target.
    """
    new_tickets = []
    unacknowledged = []
    sla_risk = []

    new_since = previous_business_day(now)

    # SLA target resolution times by priority (business days).
    # Alert fires 1 business day before the deadline.
    # Medium/Low/Default have no measurable SLA target.
    sla_business_days = {
        "Highest": 2,
        "High": 10,
    }

    todo_ids, closed_ids, awaiting_ids = build_status_sets(support_board_config)
    excluded_ids = closed_ids | awaiting_ids

    highest_priority = []

    for ticket in support_tickets:
        created_dt = parse_dt(ticket.get("created"))
        if not created_dt:
            continue

        status_id = str(ticket.get("status_id", ""))
        is_todo = status_id in todo_ids if todo_ids else False
        is_closed = status_id in closed_ids if closed_ids else False
        is_excluded = status_id in excluded_ids if excluded_ids else False
        priority = ticket.get("priority", "Medium")

        # New / Unacknowledged: only To do tickets
        if is_todo:
            created_date = created_dt.date() if hasattr(created_dt, 'date') else created_dt
            if created_date >= new_since:
                new_tickets.append(ticket)

            hours_open = (now - created_dt).total_seconds() / 3600
            if hours_open > 24:
                unacknowledged.append({
                    **ticket,
                    "hours_open": round(hours_open, 1),
                })

        # SLA risk: any non-closed ticket with a measurable SLA target
        if not is_closed and priority in sla_business_days:
            target_days = sla_business_days[priority]
            days_elapsed = business_days_since(created_dt, now)
            if days_elapsed is not None:
                days_remaining = target_days - days_elapsed
                # Alert 1 business day before the deadline (or if already past)
                if days_remaining <= 1.0:
                    sla_risk.append({
                        **ticket,
                        "days_remaining": round(days_remaining, 1),
                        "sla_days": target_days,
                        "days_elapsed": round(days_elapsed, 1),
                    })

        # Highest priority: not closed or awaiting customer
        if priority == "Highest" and not is_excluded:
            days_open = business_days_since(created_dt, now)
            highest_priority.append({
                **ticket,
                "days_open": round(days_open, 1) if days_open else 0,
            })

    return {
        "new": new_tickets,
        "unacknowledged": unacknowledged,
        "sla_risk": sla_risk,
        "highest_priority": highest_priority,
    }


def main():
    # Load fetched data
    try:
        with open("/tmp/sprint_pulse_data.json", "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("ERROR: /tmp/sprint_pulse_data.json not found. Run fetch.py first.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    sprint_start = parse_dt(data.get("sprint", {}).get("start_date"))

    issues = data.get("issues", [])
    support_tickets = data.get("support_tickets", [])

    # Load support board config if available
    support_board_config = None
    try:
        with open("/tmp/sprint_pulse_setup.json", "r") as f:
            setup_data = json.load(f)
        support_board_config = setup_data.get("support_board_config")
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Run alert rules
    stale_alerts = analyze_stale_items(issues, now)
    support_alerts = analyze_support_tickets(support_tickets, now, sprint_start, support_board_config)

    # Build output
    alerts = {
        "generated_at": now.isoformat(),
        "sprint": data.get("sprint", {}),
        "stale_items": stale_alerts,
        "support_tickets": support_alerts,
        "summary": data.get("summary", {}),
    }

    with open("/tmp/sprint_pulse_alerts.json", "w") as f:
        json.dump(alerts, f, indent=2, default=str)

    # Write pre-filtered comment data for the outstanding questions sub-agent.
    # Only includes issues/tickets that have comments or MR notes, stripping
    # all other fields to keep the file small and focused.
    comment_items = []
    for issue in issues:
        has_comments = bool(issue.get("comments"))
        mr_notes = []
        mr_links = []
        for mr in issue.get("merge_requests", []):
            if mr.get("notes"):
                mr_notes.extend(mr["notes"])
                mr_links.append({"iid": mr["iid"], "web_url": mr.get("web_url", "")})
        if has_comments or mr_notes:
            comment_items.append({
                "type": "sprint_issue",
                "key": issue["key"],
                "summary": issue.get("summary", ""),
                "is_active": issue.get("is_active", False),
                "comments": issue.get("comments", []),
                "mr_notes": mr_notes,
                "mr_links": mr_links,
            })
    for ticket in support_tickets:
        if ticket.get("comments"):
            comment_items.append({
                "type": "support_ticket",
                "key": ticket["key"],
                "summary": ticket.get("summary", ""),
                "status": ticket.get("status", ""),
                "comments": ticket.get("comments", []),
            })
    with open("/tmp/sprint_pulse_comments.json", "w") as f:
        json.dump(comment_items, f, indent=2, default=str)

    # Print summary
    stale_count = len(stale_alerts)
    support_new = len(support_alerts["new"])
    support_unack = len(support_alerts["unacknowledged"])
    support_sla = len(support_alerts["sla_risk"])

    support_highest = len(support_alerts["highest_priority"])

    print("ANALYZE_COMPLETE|stale:%d|support_new:%d|support_unack:%d|support_sla:%d|support_highest:%d" % (
        stale_count, support_new, support_unack, support_sla, support_highest
    ))

    if stale_count:
        print("\nStale items:", file=sys.stderr)
        for a in stale_alerts:
            print("  %s — %s (%.1f days)" % (a["key"], a["summary"][:50], a["days_stale"] or 0), file=sys.stderr)

    if support_new:
        print("\nNew support tickets today: %d" % support_new, file=sys.stderr)
    if support_unack:
        print("Unacknowledged support tickets: %d" % support_unack, file=sys.stderr)
    if support_sla:
        print("Support tickets at SLA risk: %d" % support_sla, file=sys.stderr)
    if support_highest:
        print("Highest priority support tickets: %d" % support_highest, file=sys.stderr)


if __name__ == "__main__":
    main()
