#!/usr/bin/env python3
"""Sprint pulse setup: validates env, discovers active sprint, parses team config."""

import json
import os
import sys

import _libpath  # noqa: F401
from jira_client import load_env, init_auth, jira_get


def main():
    required = [
        "OBSIDIAN_TEAMS_PATH",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "SPRINT_TEAMS",
        "GITLAB_URL",
        "GITLAB_TOKEN",
        "GITLAB_PROJECT_ID",
    ]
    env = load_env(required)

    missing = [v for v in required if not env.get(v)]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    base_url, auth = init_auth(env)
    teams_path = env["OBSIDIAN_TEAMS_PATH"]

    # Optional support config
    support_project_key = os.environ.get("SUPPORT_PROJECT_KEY", "")
    support_board_id = os.environ.get("SUPPORT_BOARD_ID", "")
    support_team_label = os.environ.get("SUPPORT_TEAM_LABEL", "")
    support_team_field = os.environ.get("SUPPORT_TEAM_FIELD_VALUES", "")

    print("=== ENV ===")
    print("TEAMS: " + teams_path)
    print("JIRA: " + base_url)
    print("AUTH: " + env["JIRA_EMAIL"])
    print("GITLAB: " + env["GITLAB_URL"])
    if support_project_key:
        print("SUPPORT: project=%s board=%s (labels: %s) (team field: %s)" % (
            support_project_key, support_board_id or "none", support_team_label or "none", support_team_field or "none"))
    print()

    # Parse teams
    # SUPPORT_TEAM_LABEL format: pipe-delimited per team (matching SPRINT_TEAMS order).
    # Each team slot can have multiple comma-separated labels.
    # Example: "label-a,label-b|label-c"
    #   → team 0 matches labels label-a OR label-b
    #   → team 1 matches label label-c
    # SUPPORT_TEAM_FIELD_VALUES format: same pipe-delimited structure for cf[10600] Team field.
    # Example: "TeamA,TeamB|TeamC"
    teams = []
    support_labels = [l.strip() for l in support_team_label.split("|")] if support_team_label else []
    support_field_values = [v.strip() for v in support_team_field.split("|")] if support_team_field else []

    for idx, t in enumerate(env["SPRINT_TEAMS"].split(",")):
        parts = t.strip().split("|")
        if len(parts) == 4:
            vault_dir, project_key, board_id, display_name = parts
            team_path = os.path.join(teams_path, vault_dir)
            team = {
                "vault_dir": vault_dir,
                "project_key": project_key,
                "board_id": board_id,
                "display_name": display_name,
                "path_exists": os.path.isdir(team_path),
            }
            # Map support labels by position (matching SPRINT_TEAMS order)
            if idx < len(support_labels) and support_labels[idx]:
                team["support_label"] = support_labels[idx]
            # Map Team field values by position
            if idx < len(support_field_values) and support_field_values[idx]:
                team["support_team_field"] = support_field_values[idx]
            teams.append(team)

    print("=== TEAMS ===")
    for t in teams:
        status = "OK" if t["path_exists"] else "MISSING"
        label_info = " [support: %s]" % t["support_label"] if t.get("support_label") else ""
        field_info = " [team field: %s]" % t["support_team_field"] if t.get("support_team_field") else ""
        print(
            "%s|%s|%s|%s [%s]%s%s"
            % (t["vault_dir"], t["project_key"], t["board_id"], t["display_name"], status, label_info, field_info)
        )
    print()

    # Discover boards if needed
    for t in teams:
        if not t["board_id"]:
            try:
                data = jira_get(
                    base_url,
                    "/rest/agile/1.0/board?projectKeyOrId=" + t["project_key"] + "&type=scrum",
                    auth,
                )
                if data.get("values"):
                    board = data["values"][0]
                    t["board_id"] = str(board["id"])
                    print("Discovered board for %s: %s (%s)" % (t["project_key"], t["board_id"], board["name"]))
            except Exception as e:
                print("WARNING: Could not discover board for %s: %s" % (t["project_key"], e))

    # Fetch active sprint for each team
    active_sprints = {}
    for t in teams:
        if not t["board_id"]:
            continue
        try:
            path = "/rest/agile/1.0/board/%s/sprint?state=active" % t["board_id"]
            data = jira_get(base_url, path, auth)
            sprints = data.get("values", [])
            if sprints:
                active_sprints[t["vault_dir"]] = sprints[0]
            else:
                print("WARNING: No active sprint found for %s" % t["display_name"])
                active_sprints[t["vault_dir"]] = None
        except Exception as e:
            print("WARNING: Could not fetch active sprint for %s: %s" % (t["display_name"], e))
            active_sprints[t["vault_dir"]] = None

    print("=== ACTIVE SPRINTS ===")
    for team_key, sprint in active_sprints.items():
        team_info = next(t for t in teams if t["vault_dir"] == team_key)
        if sprint:
            goal = (sprint.get("goal") or "")[:80].replace("\n", " ").replace("\r", "")
            start = sprint.get("startDate", "N/A")[:10]
            end = sprint.get("endDate", "N/A")[:10]
            print("%s: %s|%s|%s - %s|%s" % (team_info["display_name"], sprint["id"], sprint["name"], start, end, goal))
        else:
            print("%s: No active sprint" % team_info["display_name"])

    # Fetch board configuration for column mapping
    board_configs = {}
    for t in teams:
        if not t["board_id"]:
            continue
        try:
            path = "/rest/agile/1.0/board/%s/configuration" % t["board_id"]
            config = jira_get(base_url, path, auth)
            columns = []
            for col in config.get("columnConfig", {}).get("columns", []):
                statuses = [s.get("id", "") for s in col.get("statuses", [])]
                columns.append({
                    "name": col.get("name", ""),
                    "statuses": statuses,
                })
            board_configs[t["vault_dir"]] = columns
        except Exception as e:
            print("WARNING: Could not fetch board config for %s: %s" % (t["display_name"], e))
            board_configs[t["vault_dir"]] = []

    # Fetch support board configuration if board ID is set
    support_board_config = []
    if support_board_id:
        try:
            path = "/rest/agile/1.0/board/%s/configuration" % support_board_id
            config = jira_get(base_url, path, auth)
            for col in config.get("columnConfig", {}).get("columns", []):
                statuses = [s.get("id", "") for s in col.get("statuses", [])]
                support_board_config.append({
                    "name": col.get("name", ""),
                    "statuses": statuses,
                })
            col_names = [c["name"] for c in support_board_config]
            print("=== SUPPORT BOARD COLUMNS ===")
            print(" → ".join(col_names))
            print()
        except Exception as e:
            print("WARNING: Could not fetch support board config (board %s): %s" % (support_board_id, e))

    # Save setup data
    setup_data = {
        "env": {
            "teams_path": teams_path,
            "base_url": base_url,
            "email": env["JIRA_EMAIL"],
            "support_project_key": support_project_key,
            "support_board_id": support_board_id,
        },
        "gitlab": {
            "url": env["GITLAB_URL"],
            "project_id": env["GITLAB_PROJECT_ID"],
        },
        "teams": teams,
        "active_sprints": {
            k: v for k, v in active_sprints.items()
        },
        "board_configs": board_configs,
        "support_board_config": support_board_config,
    }
    with open("/tmp/sprint_pulse_setup.json.tmp", "w") as f:
        json.dump(setup_data, f, indent=2, default=str)
    os.replace("/tmp/sprint_pulse_setup.json.tmp", "/tmp/sprint_pulse_setup.json")
    print("\nSetup data saved to /tmp/sprint_pulse_setup.json")


if __name__ == "__main__":
    main()
