#!/usr/bin/env python3
"""Shared Bonusly API client utilities for bonusly-sync scripts."""

import json
import os
import sys
import urllib.request
import urllib.parse


def load_env(keys):
    """Load specific environment variables from the shell environment.

    Args:
        keys: list of variable names to extract

    Returns:
        dict mapping variable names to values
    """
    return {k: os.environ.get(k, "") for k in keys}


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
