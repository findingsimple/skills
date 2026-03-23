#!/usr/bin/env python3
"""Shared Bonusly API client utilities for bonusly-sync scripts."""

import json
import subprocess
import sys
import urllib.request
import urllib.parse


def load_env(keys):
    """Load specific environment variables from ~/.bonusly_env and ~/.obsidian_env.

    Args:
        keys: list of variable names to extract

    Returns:
        dict mapping variable names to values
    """
    print_vars = " ".join('"$%s"' % k for k in keys)
    result = subprocess.run(
        ["bash", "-c", "source ~/.bonusly_env && source ~/.obsidian_env && printf '%%s\\n' %s" % print_vars],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print("ERROR: Failed to load env files: %s" % result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    values = result.stdout.splitlines()
    return dict(zip(keys, values))


def bonusly_get(token, path, params=None):
    """GET a JSON response from the Bonusly API."""
    url = "https://bonus.ly/api/v1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def bonusly_get_all(token, path, params):
    """GET with automatic pagination. Returns all results from the 'result' array."""
    params = dict(params)
    params["limit"] = 100
    params["skip"] = 0
    all_results = []
    while True:
        data = bonusly_get(token, path, params)
        results = data.get("result", [])
        all_results.extend(results)
        if len(results) < 100:
            break
        params["skip"] += 100
    return all_results
