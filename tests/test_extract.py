"""Tests for mimik.extract (ported from upstream mimic)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import extract


def flow(host, headers, path="/v1/x", scheme="https", port=443):
    return {
        "id": "f1",
        "request": {
            "host": host, "method": "GET", "path": path,
            "scheme": scheme, "port": port,
            "headers": [[k, v] for k, v in headers],
        },
        "response": {"status_code": 200},
    }


class ExtractTests(unittest.TestCase):
    def test_keep_auth_and_device_headers(self):
        flows = [flow("api.example.com", [
            ("Authorization", "Bearer abc"),
            ("X-Device-Id", "d1"),
            ("User-Agent", "App/1.0"),
            ("Accept", "application/json"),
        ])]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(h["Authorization"], "Bearer abc")
        self.assertEqual(h["X-Device-Id"], "d1")
        self.assertEqual(h["User-Agent"], "App/1.0")
        self.assertEqual(h["Accept"], "application/json")

    def test_drop_noise_headers(self):
        flows = [flow("api.example.com", [
            ("Authorization", "Bearer abc"),
            ("Content-Length", "12"),
            ("Host", "api.example.com"),
            ("Connection", "keep-alive"),
            ("Accept-Encoding", "gzip"),
            ("X-Forwarded-For", "1.2.3.4"),
        ])]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(set(h), {"Authorization"})

    def test_newest_authed_wins(self):
        flows = [
            flow("api.example.com", [("Authorization", "Bearer old")]),
            flow("api.example.com", [("Authorization", "Bearer new")]),
        ]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(h["Authorization"], "Bearer new")

    def test_skips_unauthed_requests(self):
        flows = [
            flow("api.example.com", [("Authorization", "Bearer abc")]),
            flow("api.example.com", [("Accept", "application/json")]),
        ]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(h["Authorization"], "Bearer abc")

    def test_cookie_counts_as_authed(self):
        flows = [flow("api.example.com", [("Cookie", "s=1")])]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(h["Cookie"], "s=1")

    def test_no_authed_returns_empty(self):
        flows = [flow("api.example.com", [("Accept", "text/html")])]
        self.assertEqual(extract.session_headers(flows, "api.example.com"), {})

    def test_other_host_ignored(self):
        flows = [flow("other.example.com",
                      [("Authorization", "Bearer abc")])]
        self.assertEqual(extract.session_headers(flows, "api.example.com"), {})

    def test_base_url_https_default(self):
        flows = [flow("api.example.com", [])]
        self.assertEqual(extract.base_url(flows, "api.example.com"),
                         "https://api.example.com")

    def test_base_url_with_port(self):
        flows = [flow("api.example.com", [], port=8443)]
        self.assertEqual(extract.base_url(flows, "api.example.com"),
                         "https://api.example.com:8443")

    def test_drop_request_scoped_x_headers(self):
        """Item 32e: per-request x- headers (ids, traces, forwarded-*)
        are NOT replayed — only identity-ish x- headers survive."""
        flows = [flow("api.example.com", [
            ("Authorization", "Bearer <redacted>"),
            ("X-Device-Id", "d1"),
            ("X-Api-Key", "fake-key"),
            ("X-Request-Id", "req-1"),
            ("X-Correlation-Id", "corr-1"),
            ("X-Trace-Id", "trace-1"),
            ("X-Amzn-Trace-Id", "Root=1"),
            ("X-B3-TraceId", "b3-1"),
            ("X-Forwarded-Proto", "https"),
            ("X-Real-Ip", "1.2.3.4"),
        ])]
        h = extract.session_headers(flows, "api.example.com")
        self.assertEqual(set(h), {"Authorization", "X-Device-Id",
                                  "X-Api-Key"})

    def test_base_url_missing_host(self):
        self.assertEqual(extract.base_url([], "nope.example.com"),
                         "https://nope.example.com")


class HeaderNormalizerTests(unittest.TestCase):
    """Item 32c: the one header normalizer."""

    def test_pairs_accepts_list_and_dict_forms(self):
        self.assertEqual(
            extract.header_pairs([["A", "1"], {"name": "B", "value": "2"}]),
            [("A", "1"), ("B", "2")])

    def test_pairs_skips_malformed_entries(self):
        self.assertEqual(
            extract.header_pairs([["A", "1"], ["lonely"], "junk",
                                  ["B", "2", "extra"], None, 7,
                                  {"name": "C"}]),
            [("A", "1"), ("C", "")])

    def test_dict_lowercases_and_last_wins(self):
        self.assertEqual(
            extract.header_dict([["Content-Type", "a"],
                                 ["content-type", "b"]]),
            {"content-type": "b"})
        self.assertEqual(
            extract.header_dict([["X-A", "1"]], lower=False),
            {"X-A": "1"})

    def test_flow_pairs(self):
        f = flow("api.example.com", [("Authorization", "Bearer <redacted>")])
        self.assertEqual(
            extract.flow_header_pairs(f, "request"),
            [("Authorization", "Bearer <redacted>")])
        self.assertEqual(extract.flow_header_pairs(f, "response"), [])
        self.assertEqual(extract.flow_header_pairs({}, "request"), [])

    def test_session_headers_survives_malformed_pairs(self):
        f = flow("api.example.com", [("Authorization", "Bearer <redacted>")])
        f["request"]["headers"].append(["lonely"])
        f["request"]["headers"].append("junk")
        h = extract.session_headers([f], "api.example.com")
        self.assertEqual(h["Authorization"], "Bearer <redacted>")


if __name__ == "__main__":
    unittest.main()
