#!/usr/bin/env python3
"""Tests for the shared HTTP retry helper in _lib/_http.py.

Run with `python3 _lib/test_http.py` from the skills repo root, or from
inside _lib/. No network access — urllib.request.urlopen and time.sleep
are monkey-patched.
"""

import io
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _http


class _FakeResponse:
    def __init__(self, body=b"ok"):
        self._body = body

    def read(self):
        return self._body


def _make_http_error(code, retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        url="https://example.test/api/x?token=secret",
        code=code,
        msg="boom",
        hdrs=headers,
        fp=io.BytesIO(b""),
    )


class _Sequence:
    """Callable that returns/raises successive scripted outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, req, timeout=30):
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("urlopen called more times than scripted (%d)" % self.calls)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _SleepRecorder:
    def __init__(self):
        self.sleeps = []

    def __call__(self, seconds):
        self.sleeps.append(seconds)


class HTTPRetryTests(unittest.TestCase):
    def setUp(self):
        self._orig_urlopen = urllib.request.urlopen
        self._orig_sleep = _http.time.sleep
        self.sleep_recorder = _SleepRecorder()
        _http.time.sleep = self.sleep_recorder

    def tearDown(self):
        urllib.request.urlopen = self._orig_urlopen
        _http.time.sleep = self._orig_sleep

    def _req(self):
        return urllib.request.Request("https://example.test/api/x?token=secret")

    # -- success / no-retry ----------------------------------------------

    def test_success_first_attempt_no_sleep(self):
        urllib.request.urlopen = _Sequence([_FakeResponse(b"hello")])
        resp = _http.urlopen_with_retry(self._req())
        self.assertEqual(resp.read(), b"hello")
        self.assertEqual(self.sleep_recorder.sleeps, [])

    def test_non_retryable_http_error_raises_immediately(self):
        seq = _Sequence([_make_http_error(400)])
        urllib.request.urlopen = seq
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _http.urlopen_with_retry(self._req())
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(seq.calls, 1)
        self.assertEqual(self.sleep_recorder.sleeps, [])

    # -- 429 / 503 retry behaviour ---------------------------------------

    def test_429_then_success_uses_retry_after(self):
        seq = _Sequence([_make_http_error(429, retry_after="7"), _FakeResponse()])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req())
        self.assertEqual(seq.calls, 2)
        self.assertEqual(self.sleep_recorder.sleeps, [7.0])

    def test_503_without_retry_after_uses_exponential_backoff(self):
        # Fail twice (503) then succeed — sleeps should be base*2^0, base*2^1.
        seq = _Sequence([
            _make_http_error(503),
            _make_http_error(503),
            _FakeResponse(),
        ])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req(), base_delay=2.0)
        self.assertEqual(seq.calls, 3)
        self.assertEqual(self.sleep_recorder.sleeps, [2.0, 4.0])

    def test_retry_after_garbage_falls_back_to_backoff(self):
        seq = _Sequence([_make_http_error(429, retry_after="not-a-number"), _FakeResponse()])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req(), base_delay=1.5)
        # First retry → base * 2^0 = 1.5
        self.assertEqual(self.sleep_recorder.sleeps, [1.5])

    def test_retry_after_http_date_falls_back_to_backoff(self):
        # RFC 7231 allows HTTP-date in Retry-After. We don't parse it; we
        # should fall through to exponential backoff rather than crash.
        seq = _Sequence([
            _make_http_error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"),
            _FakeResponse(),
        ])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req(), base_delay=1.0)
        self.assertEqual(self.sleep_recorder.sleeps, [1.0])

    def test_429_exhausts_retries_and_raises_original_httperror(self):
        # max_retries=2 → 3 attempts total, 2 sleeps, then raise.
        outcomes = [_make_http_error(429) for _ in range(3)]
        seq = _Sequence(outcomes)
        urllib.request.urlopen = seq
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _http.urlopen_with_retry(self._req(), max_retries=2, base_delay=1.0)
        self.assertEqual(ctx.exception.code, 429)
        self.assertEqual(seq.calls, 3)
        self.assertEqual(self.sleep_recorder.sleeps, [1.0, 2.0])

    # -- network error retry ---------------------------------------------

    def test_network_error_retried_then_success(self):
        seq = _Sequence([
            urllib.error.URLError("conn reset"),
            TimeoutError("slow"),
            _FakeResponse(),
        ])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req(), base_delay=1.0)
        self.assertEqual(seq.calls, 3)
        self.assertEqual(self.sleep_recorder.sleeps, [1.0, 2.0])

    def test_network_error_exhausted_raises(self):
        seq = _Sequence([urllib.error.URLError("nope") for _ in range(4)])
        urllib.request.urlopen = seq
        with self.assertRaises(urllib.error.URLError):
            _http.urlopen_with_retry(self._req(), max_retries=3, base_delay=1.0)
        self.assertEqual(seq.calls, 4)
        self.assertEqual(self.sleep_recorder.sleeps, [1.0, 2.0, 4.0])

    def test_oserror_retried(self):
        seq = _Sequence([OSError("EPIPE"), _FakeResponse()])
        urllib.request.urlopen = seq
        _http.urlopen_with_retry(self._req(), base_delay=0.5)
        self.assertEqual(seq.calls, 2)
        self.assertEqual(self.sleep_recorder.sleeps, [0.5])

    # -- log-line redaction ----------------------------------------------

    def test_retry_log_line_redacts_query_string(self):
        seq = _Sequence([_make_http_error(429), _FakeResponse()])
        urllib.request.urlopen = seq
        captured = io.StringIO()
        orig_stderr = sys.stderr
        sys.stderr = captured
        try:
            _http.urlopen_with_retry(self._req())
        finally:
            sys.stderr = orig_stderr
        log = captured.getvalue()
        self.assertIn("https://example.test/api/x", log)
        self.assertNotIn("token=secret", log)
        self.assertNotIn("?", log)

    def test_network_error_log_line_redacts_query_string(self):
        # Mirrors the HTTPError redaction test but on the URLError branch.
        # Without redaction here, `?token=...` would leak via stderr.
        seq = _Sequence([urllib.error.URLError("conn reset"), _FakeResponse()])
        urllib.request.urlopen = seq
        captured = io.StringIO()
        orig_stderr = sys.stderr
        sys.stderr = captured
        try:
            _http.urlopen_with_retry(self._req())
        finally:
            sys.stderr = orig_stderr
        log = captured.getvalue()
        self.assertIn("https://example.test/api/x", log)
        self.assertNotIn("token=secret", log)
        self.assertNotIn("?", log)


class RedactURLTests(unittest.TestCase):
    def test_strips_query_string(self):
        self.assertEqual(
            _http.redact_url("https://example.test/api?key=abc&q=def"),
            "https://example.test/api",
        )

    def test_no_query_returned_unchanged(self):
        self.assertEqual(
            _http.redact_url("https://example.test/api"),
            "https://example.test/api",
        )

    def test_empty_input(self):
        self.assertEqual(_http.redact_url(""), "")
        self.assertIsNone(_http.redact_url(None))


if __name__ == "__main__":
    unittest.main()
