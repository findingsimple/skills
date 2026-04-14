#!/usr/bin/env python3
"""Bonusly sync: fetches recognition data, generates per-person markdown + sync log."""

import json
import os
import sys
import argparse
from datetime import datetime, timezone

from bonusly_client import load_env, bonusly_get_all


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--people-file", required=True, help="JSON file with [{name, email, dir}, ...]")
    p.add_argument("--start-time", required=True, help="ISO start time e.g. 2026-02-01T00:00:00Z")
    p.add_argument("--end-time", required=True, help="ISO end time e.g. 2026-03-01T00:00:00Z")
    p.add_argument("--period", required=True, help="Period label for filenames e.g. 2026-02")
    p.add_argument("--period-label", required=True, help="Human-readable period e.g. February 2026")
    p.add_argument("--teams-base", required=True, help="Path to teams base directory for sync log")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def fetch_bonuses(token, email, start_time, end_time):
    """Fetch received and given bonuses for one person."""
    received = bonusly_get_all(token, "/bonuses", {
        "start_time": start_time,
        "end_time": end_time,
        "receiver_email": email,
        "include_children": "true",
    })
    given = bonusly_get_all(token, "/bonuses", {
        "start_time": start_time,
        "end_time": end_time,
        "giver_email": email,
        "include_children": "true",
    })
    return received, given


def format_date(created_at):
    """Extract YYYY-MM-DD from a created_at timestamp."""
    return created_at[:10]


def generate_person_markdown(period, period_label, received, given, person_name=""):
    """Generate markdown content for one person's Bonusly file."""
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = []
    lines.append("---")
    lines.append("source: bonusly")
    lines.append('period: "%s"' % period)
    lines.append('generated_at: "%s"' % now_utc)
    lines.append('person: "[[%s]]"' % person_name)
    lines.append("---")
    lines.append("")
    lines.append("# Bonusly Recognition — %s" % period_label)

    if received:
        total_pts = sum(b.get("amount", 0) for b in received)
        lines.append("")
        lines.append("## Received (%d smiles)" % total_pts)
        lines.append("")
        for b in sorted(received, key=lambda b: b.get("created_at", "")):
            date = format_date(b.get("created_at", ""))
            amount = b.get("amount", 0)
            giver = (b.get("giver") or {}).get("full_name", "Unknown")
            reason = b.get("reason", "").strip()
            lines.append('- **%s** — +%d from **[[%s]]**: "%s"' % (date, amount, giver, reason))
            for child in b.get("child_bonuses", []):
                child_giver = (child.get("giver") or {}).get("full_name", "Unknown")
                child_amount = child.get("amount", 0)
                child_reason = (child.get("reason") or "").strip()
                if child_reason:
                    lines.append('  - +%d from **[[%s]]**: "%s"' % (child_amount, child_giver, child_reason))
                else:
                    lines.append("  - +%d from **[[%s]]**" % (child_amount, child_giver))

    if given:
        total_pts = sum(b.get("amount", 0) for b in given)
        lines.append("")
        lines.append("## Given (%d smiles)" % total_pts)
        lines.append("")
        for b in sorted(given, key=lambda b: b.get("created_at", "")):
            date = format_date(b.get("created_at", ""))
            amount = b.get("amount", 0)
            receiver = (b.get("receiver") or {}).get("full_name", "Unknown")
            reason = b.get("reason", "").strip()
            lines.append('- **%s** — +%d to **[[%s]]**: "%s"' % (date, amount, receiver, reason))

    return "\n".join(lines) + "\n"


def main():
    args = parse_args()
    env = load_env(["BONUSLY_API_TOKEN"])

    token = env.get("BONUSLY_API_TOKEN", "")
    if not token:
        print("ERROR: BONUSLY_API_TOKEN not set — add it to ~/.zshrc")
        sys.exit(1)

    with open(args.people_file, "r") as f:
        people = json.load(f)

    period = args.period
    period_label = args.period_label
    teams_base = args.teams_base
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log_rows = []
    files_created = 0
    files_skipped = 0

    for person in people:
        name = person["name"]
        email = person["email"]
        person_dir = person["dir"]

        print("Fetching bonuses for %s (%s)..." % (name, email), file=sys.stderr)

        try:
            received, given = fetch_bonuses(token, email, args.start_time, args.end_time)
        except Exception as e:
            print("  ERROR: %s" % e, file=sys.stderr)
            log_rows.append({"name": name, "received_count": 0, "received_pts": 0,
                             "given_count": 0, "given_pts": 0, "status": "Error"})
            continue

        received_count = len(received)
        given_count = len(given)
        received_pts = sum(b.get("amount", 0) for b in received)
        given_pts = sum(b.get("amount", 0) for b in given)

        if received_count == 0 and given_count == 0:
            log_rows.append({"name": name, "received_count": 0, "received_pts": 0,
                             "given_count": 0, "given_pts": 0, "status": "Skipped"})
            files_skipped += 1
            continue

        md = generate_person_markdown(period, period_label, received, given, name)
        file_path = os.path.join(person_dir, "Feedback", "Bonusly - %s.md" % period)

        if args.dry_run:
            print("\n--- Would write to: %s ---" % file_path)
            print(md)
            log_rows.append({"name": name, "received_count": received_count, "received_pts": received_pts,
                             "given_count": given_count, "given_pts": given_pts, "status": "Would create"})
        else:
            os.makedirs(os.path.join(person_dir, "Feedback"), exist_ok=True)
            tmp_file = file_path + ".tmp"
            with open(tmp_file, "w") as f:
                f.write(md)
            os.replace(tmp_file, file_path)
            log_rows.append({"name": name, "received_count": received_count, "received_pts": received_pts,
                             "given_count": given_count, "given_pts": given_pts, "status": "Created"})
        files_created += 1

    # Generate sync log
    log_lines = []
    log_lines.append("---")
    log_lines.append("source: bonusly")
    log_lines.append("type: sync-log")
    log_lines.append('period: "%s"' % period)
    log_lines.append('synced_at: "%s"' % now_utc)
    log_lines.append("---")
    log_lines.append("")
    log_lines.append("# Bonusly Sync — %s" % period_label)
    log_lines.append("")
    log_lines.append("| Person | Received | Given | File |")
    log_lines.append("|--------|----------|-------|------|")
    for row in log_rows:
        rec_str = "%d (%d pts)" % (row["received_count"], row["received_pts"]) if row["received_count"] else "0"
        giv_str = "%d (%d pts)" % (row["given_count"], row["given_pts"]) if row["given_count"] else "0"
        log_lines.append("| [[%s]] | %s | %s | %s |" % (row["name"], rec_str, giv_str, row["status"]))
    log_lines.append("")
    log_lines.append("**%d files created**, %d skipped. %d people processed." % (
        files_created, files_skipped, len(people)))
    log_md = "\n".join(log_lines) + "\n"

    log_path = os.path.join(teams_base, "Logs", "Bonusly Sync - %s.md" % period)
    if args.dry_run:
        print("\n--- Would write sync log to: %s ---" % log_path)
        print(log_md)
    else:
        os.makedirs(os.path.join(teams_base, "Logs"), exist_ok=True)
        tmp_file = log_path + ".tmp"
        with open(tmp_file, "w") as f:
            f.write(log_md)
        os.replace(tmp_file, log_path)

    # Output summary
    if args.dry_run:
        print("\n**DRY RUN** — no files were written.")
    else:
        print("\nBonusly sync complete for %s:" % period_label)
    print("| Person | Received | Given | File |")
    print("|--------|----------|-------|------|")
    for row in log_rows:
        rec_str = "%d (%d pts)" % (row["received_count"], row["received_pts"]) if row["received_count"] else "0"
        giv_str = "%d (%d pts)" % (row["given_count"], row["given_pts"]) if row["given_count"] else "0"
        print("| %s | %s | %s | %s |" % (row["name"], rec_str, giv_str, row["status"]))
    print("\n%d files %s, %d skipped. %d people processed." % (
        files_created, "previewed" if args.dry_run else "created", files_skipped, len(people)))


if __name__ == "__main__":
    main()
