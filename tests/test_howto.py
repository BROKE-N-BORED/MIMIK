"""Tests for mimik/howto.py — the How to Use index (v0.5 item 13)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import howto


class HowtoTests(unittest.TestCase):
    def test_every_tab_has_a_topic(self):
        titles = [title for _key, title in howto.topics()]
        for tab in ("Record", "Hosts and Learn", "Generate", "Assets",
                    "Clients", "Binary", "Doctor", "Presets"):
            self.assertTrue(any(tab in t for t in titles),
                            f"no topic for the {tab} tab")

    def test_every_topic_has_text(self):
        for key, _title in howto.topics():
            text = howto.body(key)
            self.assertIsInstance(text, str)
            self.assertGreater(len(text), 100, key)

    def test_keys_are_unique(self):
        keys = [key for key, _title in howto.topics()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_unknown_key_is_none(self):
        self.assertIsNone(howto.body("no-such-topic"))

    def test_no_em_dash_in_topic_text(self):
        # U33: no em-dashes in visible UI strings.
        for key, title in howto.topics():
            self.assertNotIn("\u2014", title, key)
            self.assertNotIn("\u2014", howto.body(key), key)


if __name__ == "__main__":
    unittest.main()
