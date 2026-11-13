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

    def test_endpoints_carry_sample_counts(self):
        # /v1/users was captured twice (page=1, page=2); /v1/messages once.
        eps = har.endpoints(FIXTURE, "api.example.com")
        by_path = {e["path"]: e for e in eps}
        self.assertEqual(by_path["/v1/users"]["samples"], 2)
        self.assertEqual(by_path["/v1/messages"]["samples"], 1)

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


class HarParseOnceTests(unittest.TestCase):
    """Item 8: parse once and share; friendly errors; bodies embedded."""

    def test_parse_once_shared(self):
        data = har.parse(MIMIK_FIXTURE)
        self.assertEqual(har.load(data), har.load(MIMIK_FIXTURE))
        self.assertEqual(har.hosts(data), har.hosts(MIMIK_FIXTURE))
        self.assertEqual(har.endpoints(data, "api.example.com"),
                         har.endpoints(MIMIK_FIXTURE, "api.example.com"))

    def test_dict_api_reads_no_file(self):
        # A parsed dict needs no file access at all — load/hosts/
        # endpoints all work from the dict alone.
        import json

        with open(MIMIK_FIXTURE) as fh:
            data = json.load(fh)
        self.assertEqual(len(har.load(data)), 5)
        self.assertEqual(har.hosts(data),
                         [("api.example.com", 4), ("cdn.example.com", 1)])
        self.assertEqual(len(har.endpoints(data, "api.example.com")), 4)

    def test_malformed_har_friendly_errors(self):
        import tempfile

        bad = tempfile.NamedTemporaryFile("w", suffix=".har", delete=False)
        bad.write("{not json")
        bad.close()
        with self.assertRaises(har.HarError) as ctx:
            har.parse(bad.name)
        self.assertIn("not valid JSON", str(ctx.exception))
        os.unlink(bad.name)

        not_har = tempfile.NamedTemporaryFile("w", suffix=".har",
                                              delete=False)
        not_har.write('{"hello": "world"}')
        not_har.close()
        with self.assertRaises(har.HarError) as ctx:
            har.parse(not_har.name)
        self.assertIn("doesn't look like a HAR file", str(ctx.exception))
        os.unlink(not_har.name)

        with self.assertRaises(har.HarError) as ctx:
            har.parse("/no/such/file.har")
        self.assertIn("no such file", str(ctx.exception))

    def test_load_embeds_bodies(self):
        flows = har.load(MIMIK_FIXTURE)
        like = [f for f in flows
                if f["request"]["path"] == "/v1/like"][0]
        self.assertIn("subjectId", like["_saved_request_body"])
        self.assertIn("liked", like["_saved_response_body"])

    def test_ids_differ_across_files_same_within(self):
        first = {f["id"] for f in har.load(FIXTURE)}
        again = {f["id"] for f in har.load(FIXTURE)}
        other = {f["id"] for f in har.load(MIMIK_FIXTURE)}
        self.assertEqual(first, again)  # re-import dedupes
        self.assertFalse(first & other)  # different files don't collide

    def test_endpoints_from_saved_matches_har_endpoints(self):
        """The CLI now reads HAR endpoints from the loaded flows
        (one parse) — pin that it equals the entry-based reader."""
        from mimik.sources import mitm as mitm_mod

        flows = har.load(MIMIK_FIXTURE)
        via_saved = mitm_mod.endpoints_from_saved(flows, "api.example.com")
        via_entries = har.endpoints(MIMIK_FIXTURE, "api.example.com")

        def key(e):
            return (e["method"], e["path"])

        self.assertEqual(sorted(map(key, via_saved)),
                         sorted(map(key, via_entries)))
        for a in via_saved:
            b = [e for e in via_entries if key(e) == key(a)][0]
            self.assertEqual(a["status"], b["status"])
            self.assertEqual(a["query"], b["query"])
            self.assertEqual(a["request_body"], b["request_body"])
            self.assertEqual(a["response_body"], b["response_body"])


if __name__ == "__main__":
    unittest.main()
