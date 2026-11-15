"""Tests for mimik/machine.py — the silent PC measurement (v0.5 item 17,
U35). The derived settings are pure functions of a RAM/CPU value, so
every boundary is testable without touching the real machine; measure()
itself is only asserted for shape (this sandbox is 8 GB, his PC is 8 GB,
but the tests must pass on any machine).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mimik import machine

GB = 1024 * 1024 * 1024


class HeapTests(unittest.TestCase):
    def test_8gb_gets_2g(self):
        self.assertEqual(machine.ghidra_heap(8 * GB), "2G")

    def test_4gb_gets_1g(self):
        self.assertEqual(machine.ghidra_heap(4 * GB), "1G")

    def test_2gb_gets_1g(self):
        self.assertEqual(machine.ghidra_heap(2 * GB), "1G")

    def test_16gb_gets_4g(self):
        self.assertEqual(machine.ghidra_heap(16 * GB), "4G")

    def test_32gb_clamps_to_4g(self):
        self.assertEqual(machine.ghidra_heap(32 * GB), "4G")

    def test_unknown_machine_gets_the_default(self):
        self.assertEqual(machine.ghidra_heap(0), "2G")


class FlowsCapTests(unittest.TestCase):
    def test_8gb_gets_5000(self):
        self.assertEqual(machine.flows_cap(8 * GB), 5000)

    def test_2gb_gets_2500(self):
        self.assertEqual(machine.flows_cap(2 * GB), 2500)

    def test_32gb_gets_10000(self):
        self.assertEqual(machine.flows_cap(32 * GB), 10000)

    def test_unknown_machine_gets_5000(self):
        self.assertEqual(machine.flows_cap(0), 5000)


class WorkersTests(unittest.TestCase):
    def test_multicore_gets_2(self):
        self.assertEqual(machine.max_workers(4), 2)

    def test_single_core_gets_1(self):
        self.assertEqual(machine.max_workers(1), 1)


class MeasureTests(unittest.TestCase):
    def setUp(self):
        machine._reset_cache()

    def tearDown(self):
        machine._reset_cache()

    def test_measure_shape(self):
        m = machine.measure()
        self.assertIn("ram_total", m)
        self.assertIn("cpu_count", m)
        self.assertIn("disk_free", m)
        self.assertIn("disk_total", m)
        self.assertGreaterEqual(m["ram_total"], 0)
        self.assertGreaterEqual(m["cpu_count"], 1)
        self.assertGreaterEqual(m["disk_free"], 0)
        self.assertGreaterEqual(m["disk_total"], m["disk_free"])

    def test_measure_is_cached(self):
        first = machine.measure()
        second = machine.measure()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_settings_carries_measurements_and_derived(self):
        s = machine.settings()
        self.assertEqual(s["ghidra_heap"],
                         machine.ghidra_heap(s["ram_total"]))
        self.assertEqual(s["flows_cap"],
                         machine.flows_cap(s["ram_total"]))
        self.assertEqual(s["max_workers"],
                         machine.max_workers(s["cpu_count"]))

    def test_describe_is_plain_words(self):
        lines = machine.describe()
        self.assertEqual(len(lines), 1)
        self.assertIn("RAM", lines[0])
        self.assertIn("CPU", lines[0])
        self.assertIn("disk free", lines[0])

    def test_log_measurements_writes_to_the_log(self):
        from mimik import log

        tmp = tempfile.mkdtemp()
        os.environ["MIMIK_DATA_DIR"] = tmp
        try:
            machine.log_measurements()
            tail = log.read_tail()
        finally:
            del os.environ["MIMIK_DATA_DIR"]
        self.assertIn("machine", tail)
        self.assertIn("Ghidra heap", tail)


if __name__ == "__main__":
    unittest.main()
