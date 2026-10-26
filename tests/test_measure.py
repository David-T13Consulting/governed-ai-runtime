import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import run_dev_cases

HERE = Path(__file__).resolve().parent.parent


class PercentileTest(unittest.TestCase):
    def test_nearest_rank_uses_ceil(self):
        """Round D finding 8: p95 of 1..11 is 11 (rank ceil(10.45) = 11), not 10."""
        self.assertEqual(run_dev_cases.percentile(list(range(1, 12)), 95), 11)
        self.assertEqual(run_dev_cases.percentile(list(range(1, 21)), 95), 19)
        self.assertEqual(run_dev_cases.percentile(list(range(1, 11)), 50), 5)
        self.assertEqual(run_dev_cases.percentile([7], 95), 7)
        self.assertIsNone(run_dev_cases.percentile([], 95))


class MeasureScriptTest(unittest.TestCase):
    def test_artifacts_inside_the_vault_are_refused_before_any_write(self):
        """Round D finding 1: measure.sh created its evidence directory and files with
        plain shell before Python refused anything."""
        tmp = Path(tempfile.mkdtemp(prefix="dbrain-measure-"))
        vault = tmp / "vault"
        vault.mkdir()
        before = sorted(p for p in vault.rglob("*"))
        env = dict(os.environ, DBRAIN_VAULT=str(vault), DBRAIN_ARTIFACTS=str(vault / "out"))
        proc = subprocess.run(["sh", str(HERE / "measure.sh"), "probe"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stderr + proc.stdout)
        self.assertEqual(sorted(p for p in vault.rglob("*")), before)

    def test_an_existing_evidence_directory_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="dbrain-measure-"))
        (tmp / "probe-evidence").mkdir()
        env = dict(os.environ, DBRAIN_ARTIFACTS=str(tmp))
        proc = subprocess.run(["sh", str(HERE / "measure.sh"), "probe"], env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list((tmp / "probe-evidence").iterdir()), [])


class SecretScanTest(unittest.TestCase):
    def test_the_scan_passes_its_own_self_tests_and_is_clean(self):
        """Round D finding 5: the self-test now also proves the error path returns 2."""
        proc = subprocess.run(["sh", str(HERE / "secret_scan.sh")], cwd=str(HERE),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class StaticGuardsTest(unittest.TestCase):
    """Round F finding 4: catch the exact regressions, not just their symptoms."""

    def _code_lines(self, rel):
        return [l for l in (HERE / rel).read_text().splitlines() if not l.lstrip().startswith("#")]

    def test_no_shell_script_appends_to_an_evidence_file(self):
        for rel in ("measure.sh", "experiments/run.sh"):
            self.assertFalse([l for l in self._code_lines(rel) if ">>" in l], rel)

    def test_the_sweep_health_check_uses_the_guarded_opener(self):
        code = "\n".join(self._code_lines("experiments/latency_sweep.py"))
        self.assertNotIn("urlopen(", code)
        self.assertIn("RefuseRedirects()", code)
        self.assertIn("ProxyHandler({})", code)

if __name__ == "__main__":
    unittest.main()
