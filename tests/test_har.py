"""Tests for mimik.sources.har + Session.from_har (upstream tests ported)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import Session
from mimik.sources import har

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample.har")
MIMIK_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mimik.har")


class HarTests(unittest.TestCase):
    def test_load_basic(self):
        flows = har.load(FIXTURE)
        self.assertEqual(len(flows), 4)
        req = flows[0]["request"]
        self.assertEqual(req["host"], "api.example.com")
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/v1/users?page=1")
        self.assertEqual(req["scheme"], "https")
        self.assertEqual(req["port"], 443)
        self.assertEqual(req["headers"][0],
                         ["Authorization", "Bearer token123"])

    def test_hosts(self):
        self.assertEqual(har.hosts(FIXTURE),
                         [("api.example.com", 3), ("cdn.example.com", 1)])

    def test_endpoints_dedup_latest_wins(self):
        eps = har.endpoints(FIXTURE, "api.example.com")
        self.assertEqual(len(eps), 2)
        users = [e for e in eps if e["path"] == "/v1/users"]
        self.assertEqual(len(users), 1)
        # latest (page=2) wins over the earlier 500
        self.assertEqual(users[0]["status"], 200)

    def test_session_from_har(self):
        s = Session.from_har(FIXTURE, "api.example.com")
        self.assertEqual(s.base_url, "https://api.example.com")
        self.assertEqual(s.host, "api.example.com")

    def test_session_from_har_picks_busiest_host(self):
        s = Session.from_har(FIXTURE)
        self.assertEqual(s.host, "api.example.com")

    def test_mimik_fixture_hosts(self):
        self.assertEqual(har.hosts(MIMIK_FIXTURE),
                         [("api.example.com", 4), ("cdn.example.com", 1)])

    def test_mimik_fixture_endpoints(self):
        eps = har.endpoints(MIMIK_FIXTURE, "api.example.com")
        paths = {(e["method"], e["path"]) for e in eps}
        self.assertIn(("GET", "/v1/users"), paths)
        self.assertIn(("POST", "/v1/messages"), paths)
        self.assertIn(("POST", "/v1/like"), paths)
        self.assertIn(("GET", "/v1/users/user-9f2c4e77a1b8"), paths)

    def test_mimik_fixture_bodies_present(self):
        eps = har.endpoints(MIMIK_FIXTURE, "api.example.com")
        like = [e for e in eps if e["path"] == "/v1/like"][0]
        self.assertIn("subjectId", like["request_body"])
        self.assertIn("liked", like["response_body"])

    def test_mimik_fixture_query_kept(self):
        eps = har.endpoints(MIMIK_FIXTURE, "api.example.com")
        users = [e for e in eps if e["path"] == "/v1/users"][0]
        self.assertEqual(users["query"], "page=1")


if __name__ == "__main__":
    unittest.main()
