"""Tests for mimik.sources.curl + Session.from_curl."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import Session
from mimik.sources import curl

CURL_GET = (
    "curl 'https://api.example.com/v1/users?page=1' "
    "-H 'Authorization: Bearer SECRETTOKEN123' "
    "-H 'User-Agent: ExampleApp/2.0'"
)
CURL_POST = (
    "curl 'https://api.example.com/v1/like' -X POST "
    "-H 'Authorization: Bearer SECRETTOKEN123' "
    "-H 'Content-Type: application/json' "
    "--data '{\"subjectId\": \"abc-123\"}'"
)


class CurlTests(unittest.TestCase):
    def test_parse_get(self):
        p = curl.parse(CURL_GET)
        self.assertEqual(p["url"], "https://api.example.com/v1/users?page=1")
        self.assertEqual(p["method"], "GET")
        self.assertEqual(p["headers"]["Authorization"],
                         "Bearer SECRETTOKEN123")
        self.assertEqual(p["data"], "")

    def test_parse_post_with_data(self):
        p = curl.parse(CURL_POST)
        self.assertEqual(p["method"], "POST")
        self.assertIn("subjectId", p["data"])

    def test_parse_data_implies_post(self):
        p = curl.parse("curl 'https://x.example.com/a' --data 'a=1'")
        self.assertEqual(p["method"], "POST")

    def test_parse_no_url_raises(self):
        with self.assertRaises(ValueError):
            curl.parse("curl -H 'A: b'")

    def test_parse_base_headers(self):
        base, headers = curl.parse_base_headers(CURL_GET)
        self.assertEqual(base, "https://api.example.com")
        self.assertEqual(headers["Authorization"], "Bearer SECRETTOKEN123")

    def test_load_as_flows(self):
        flows = curl.load(CURL_POST)
        self.assertEqual(len(flows), 1)
        req = flows[0]["request"]
        self.assertEqual(req["host"], "api.example.com")
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/v1/like")
        self.assertIn("subjectId", flows[0]["_saved_request_body"])

    def test_load_from_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".txt",
                                         delete=False) as f:
            f.write(CURL_GET)
            path = f.name
        try:
            flows = curl.load(path)
            self.assertEqual(flows[0]["request"]["host"], "api.example.com")
        finally:
            os.unlink(path)

    def test_session_from_curl(self):
        s = Session.from_curl(CURL_GET)
        self.assertEqual(s.base_url, "https://api.example.com")
        self.assertEqual(s.headers["Authorization"], "Bearer SECRETTOKEN123")


if __name__ == "__main__":
    unittest.main()
