"""Tests for the CI workflow (v0.4 item 6).

The workflow is the guard that would have caught the fresh-clone icon
defect — these tests pin its shape: both envs, the documented suite
commands, the Qt env vars. YAML parsing needs PyYAML (not a MIMIK
dependency), so the parse test skips cleanly without it.
"""
import importlib.util
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")

HAS_YAML = importlib.util.find_spec("yaml") is not None


class CiWorkflowTests(unittest.TestCase):
    def test_workflow_file_exists(self):
        self.assertTrue(os.path.exists(WORKFLOW),
                        ".github/workflows/ci.yml is missing")

    @unittest.skipUnless(HAS_YAML, "PyYAML not available")
    def test_workflow_is_valid_yaml(self):
        import yaml

        with open(WORKFLOW) as fh:
            data = yaml.safe_load(fh)
        self.assertIsInstance(data, dict)
        self.assertIn("jobs", data)
        self.assertIn("on", data)

    @unittest.skipUnless(HAS_YAML, "PyYAML not available")
    def test_workflow_runs_both_envs(self):
        import yaml

        with open(WORKFLOW) as fh:
            data = yaml.safe_load(fh)
        jobs = data["jobs"]
        self.assertIn("test-system", jobs)
        self.assertIn("test-qt", jobs)
        for name in ("test-system", "test-qt"):
            steps = jobs[name]["steps"]
            runs = [s.get("run", "") for s in steps if "run" in s]
            self.assertTrue(
                any("python -m unittest discover -s tests" in r
                    for r in runs),
                f"{name} does not run the documented suite command")

    @unittest.skipUnless(HAS_YAML, "PyYAML not available")
    def test_qt_job_sets_the_qt_env(self):
        import yaml

        with open(WORKFLOW) as fh:
            data = yaml.safe_load(fh)
        steps = data["jobs"]["test-qt"]["steps"]
        suite_steps = [s for s in steps
                       if "unittest discover" in s.get("run", "")]
        self.assertEqual(len(suite_steps), 1)
        env = suite_steps[0].get("env", {})
        self.assertEqual(str(env.get("MIMIK_TEST_QIMAGE")), "1")
        self.assertEqual(env.get("QT_QPA_PLATFORM"), "offscreen")

    @unittest.skipUnless(HAS_YAML, "PyYAML not available")
    def test_workflow_triggers_on_push_to_main(self):
        import yaml

        with open(WORKFLOW) as fh:
            data = yaml.safe_load(fh)
        on = data["on"]  # quoted in the YAML, so the key is the string
        self.assertIn("push", on)
        self.assertIn("main", on["push"]["branches"])

    def test_workflow_installs_the_deps_each_env_needs(self):
        with open(WORKFLOW) as fh:
            text = fh.read()
        self.assertIn("pip install requests", text)
        self.assertIn("pip install requests PySide6", text)
        self.assertIn("libegl1", text)


if __name__ == "__main__":
    unittest.main()
