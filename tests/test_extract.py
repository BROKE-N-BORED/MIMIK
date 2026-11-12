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

    def test_base_url_missing_host(self):
        self.assertEqual(extract.base_url([], "nope.example.com"),
                         "https://nope.example.com")


if __name__ == "__main__":
    unittest.main()
