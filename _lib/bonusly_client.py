#!/usr/bin/env python3
"""Shared Bonusly API client utilities for bonusly-sync scripts."""

import json
import os
import sys
import time
import urllib.error
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


def _redact_url(url):
    """Drop the query string from retry logs so keywords/tokens don't leak."""
    return url.split("?", 1)[0] if url else url


def _urlopen_with_retry(req, timeout=30, max_retries=3, base_delay=1.0):
    """urlopen with retry and exponential backoff for transient errors."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (429, 503) and attempt < max_retries:
                retry_after = e.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                except (ValueError, TypeError):
                    delay = base_delay * (2 ** attempt)
                print("HTTP %d, retrying in %.1fs... (%s)" % (e.code, delay, _redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print("Network error: %s, retrying in %.1fs... (%s)" % (e, delay, _redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    raise RuntimeError("urlopen retry loop exhausted without returning or raising") from last_exc


def bonusly_get(token, path, params=None):
    """GET a JSON response from the Bonusly API."""
    url = "https://bonus.ly/api/v1" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with _urlopen_with_retry(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise Exception("Bonusly API %d on %s: %s" % (e.code, path, body)) from None


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
