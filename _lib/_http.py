#!/usr/bin/env python3
"""Shared HTTP retry helper for all _lib/*_client.py modules.

Single source of truth for the urlopen-with-retry behaviour and the URL
redaction used in retry log lines. Prior to this module, jira_client,
gitlab_client, confluence_client, and bonusly_client each carried byte-
identical copies of these two helpers.
"""

import sys
import time
import urllib.error
import urllib.request


def redact_url(url):
    """Drop the query string from retry logs so keywords/tokens don't leak."""
    return url.split("?", 1)[0] if url else url


def urlopen_with_retry(req, timeout=30, max_retries=3, base_delay=1.0):
    """urlopen with retry and exponential backoff for transient errors.

    Retries on HTTP 429 / 503 (honouring Retry-After when present) and on
    network-level URLError / OSError / TimeoutError. Raises on exhausted
    retries — never returns None.
    """
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
                print("HTTP %d, retrying in %.1fs... (%s)" % (e.code, delay, redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                print("Network error: %s, retrying in %.1fs... (%s)" % (e, delay, redact_url(req.full_url)), file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    # Should be unreachable — every path above either returns or raises.
    raise RuntimeError("urlopen retry loop exhausted without returning or raising") from last_exc
