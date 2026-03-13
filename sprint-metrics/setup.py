#!/usr/bin/env python3
"""Sprint metrics setup: validates env (including GitLab), discovers boards, lists sprints."""

import subprocess
import json
import os
import sys
import urllib.request
import base64


def load_env():
    result = subprocess.run(
        ["bash", "-c", "source ~/.obsidian_env && source ~/.sprint_summary_env && env"],
        capture_output=True,
        text=True,
    )
    return dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if "=" in line
    )


def jira_get(base_url, path, auth):
    req = urllib.request.Request(
        base_url + path,
        headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    env = load_env()

    required = [
        "OBSIDIAN_VAULT_PATH",
        "OBSIDIAN_TEAMS_PATH",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "SPRINT_TEAMS",
        "GITLAB_URL",
        "GITLAB_TOKEN",
        "GITLAB_PROJECT_ID",
    ]
    missing = [v for v in required if v not in env]
    if missing:
        print("ERROR: Missing env vars: " + ", ".join(missing))
        sys.exit(1)

    vault_path = env["OBSIDIAN_VAULT_PATH"]
    teams_path = env["OBSIDIAN_TEAMS_PATH"]
    base_url = env["JIRA_BASE_URL"]
    email = env["JIRA_EMAIL"]
    token = env["JIRA_API_TOKEN"]
    gitlab_url = env["GITLAB_URL"]
    gitlab_project_id = env["GITLAB_PROJECT_ID"]
    auth = base64.b64encode((email + ":" + token).encode()).decode()

    print("=== ENV ===")
    print("VAULT: " + vault_path)
    print("TEAMS: " + teams_path)
    print("JIRA: " + base_url)
    print("AUTH: " + email)
    print("GITLAB: " + gitlab_url)
    print("GITLAB_PROJECT: " + gitlab_project_id)
    print()

    # Parse teams
    teams = []
    for t in env["SPRINT_TEAMS"].split(","):
        parts = t.strip().split("|")
        if len(parts) == 4:
            vault_dir, project_key, board_id, display_name = parts
            team_path = os.path.join(teams_path, vault_dir)
            teams.append(
                {
                    "vault_dir": vault_dir,
                    "project_key": project_key,
                    "board_id": board_id,
                    "display_name": display_name,
                    "path_exists": os.path.isdir(team_path),
                }
            )

    print("=== TEAMS ===")
    for t in teams:
        status = "OK" if t["path_exists"] else "MISSING"
        print(
            "%s|%s|%s|%s [%s]"
            % (t["vault_dir"], t["project_key"], t["board_id"], t["display_name"], status)
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

    # Fetch sprints for each team
    all_sprints = {}
    for t in teams:
        if not t["board_id"]:
            continue
        try:
            path = "/rest/agile/1.0/board/%s/sprint?state=closed&maxResults=50" % t["board_id"]
            data = jira_get(base_url, path, auth)
            sprints = data.get("values", [])
            if not data.get("isLast", True):
                total = data.get("total", len(sprints))
                if total > 50:
                    path = "/rest/agile/1.0/board/%s/sprint?state=closed&maxResults=50&startAt=%d" % (
                        t["board_id"],
                        total - 50,
                    )
                    data = jira_get(base_url, path, auth)
                    sprints = data.get("values", [])
            sprints.sort(key=lambda s: s.get("completeDate", ""), reverse=True)
            all_sprints[t["vault_dir"]] = sprints[:10]
        except Exception as e:
            print("WARNING: Could not fetch sprints for %s: %s" % (t["display_name"], e))
            all_sprints[t["vault_dir"]] = []

    print("=== SPRINTS ===")
    for team_key, sprints in all_sprints.items():
        team_info = next(t for t in teams if t["vault_dir"] == team_key)
        print("\n--- %s ---" % team_info["display_name"])
        for s in sprints[:6]:
            goal = (s.get("goal") or "")[:80].replace("\n", " ").replace("\r", "")
            end = s.get("endDate", s.get("completeDate", "N/A"))[:10]
            print("%s|%s|%s|%s" % (s["id"], s["name"], end, goal))

    setup_data = {
        "env": {
            "vault_path": vault_path,
            "teams_path": teams_path,
            "base_url": base_url,
            "email": email,
            "gitlab_url": gitlab_url,
            "gitlab_project_id": gitlab_project_id,
        },
        "teams": teams,
        "sprints": {k: v[:10] for k, v in all_sprints.items()},
    }
    with open("/tmp/sprint_metrics_setup.json", "w") as f:
        json.dump(setup_data, f, indent=2)
    print("\nSetup data saved to /tmp/sprint_metrics_setup.json")


if __name__ == "__main__":
    main()
