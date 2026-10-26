"""P2.7 step C3b (data policy §7b): the service's logs and traces live outside the vault
and outside every git working tree. Nothing here writes to the host's real logs folder."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dbrain import adapter, answer, config, outpath, trace
from tests import fake_vault

HERE = Path(__file__).resolve().parent.parent
REAL_DEFAULT = Path.home() / "Library" / "Application Support" / "dbrain" / "logs"


class DefaultLocationTest(unittest.TestCase):
    def default_logs(self):
        """config.LOGS as the service sees it, without the test guard's override."""
        env = {k: v for k, v in os.environ.items() if k != "DBRAIN_LOGS"}
        out = subprocess.run(
            [sys.executable, "-c", "from dbrain import config, failures; "
             "print(config.LOGS); print(failures.LOGS)"],
            cwd=str(HERE), env=env, capture_output=True, text=True, check=True)
        return [Path(line) for line in out.stdout.splitlines()]

    def test_the_default_is_the_hosts_logs_folder_beside_the_audit_log(self):
        logs, audit_logs = self.default_logs()
        self.assertEqual(logs, REAL_DEFAULT)
        self.assertEqual(logs, audit_logs)

    def test_the_default_is_outside_the_vault_and_outside_git(self):
        logs, _ = self.default_logs()
        self.assertFalse(str(logs).startswith(str(Path(config.VAULT).resolve()) + os.sep))
        self.assertFalse(str(logs).startswith(str(HERE) + os.sep))
        outpath.assert_outside_git(logs)      # checks only; creates nothing

    def test_tests_never_point_at_the_real_folder(self):
        self.assertNotEqual(Path(config.LOGS).resolve(), REAL_DEFAULT.resolve())


class TraceDestinationTest(unittest.TestCase):
    def setUp(self):
        self.vault, _, _ = fake_vault.build()
        model = adapter.StubAdapter(reply="Tempo means marathon pace or faster [E1]")
        self.model = model
        self.result = answer.ask("What does tempo mean?", profile=fake_vault.PROFILE,
                                 model=model, vault=self.vault)

    def test_a_trace_inside_a_git_working_tree_is_refused_and_nothing_written(self):
        repo = Path(tempfile.mkdtemp(prefix="dbrain-trace-git-"))
        (repo / ".git").mkdir()
        for root in (repo, repo / "artifacts"):           # git-ignored folders count too
            with self.subTest(root=str(root)):
                with self.assertRaises(outpath.OutsideVaultError):
                    trace.write(self.result, self.model, fake_vault.PROFILE, artifacts=root)
        self.assertEqual(sorted(p.name for p in repo.iterdir()), [".git"])

    def test_the_default_trace_goes_to_config_logs(self):
        if Path(config.LOGS).resolve() == REAL_DEFAULT.resolve():
            self.fail("refusing to write a test trace to the real logs folder")
        path = trace.write(self.result, self.model, fake_vault.PROFILE)
        self.assertEqual(Path(path).parent.resolve(), Path(config.LOGS).resolve())
        self.assertEqual(json.loads(Path(path).read_text().splitlines()[-1])["reason_code"],
                         answer.UNVERIFIED)


def outpath_cli(*args, cwd=None):
    return subprocess.run([sys.executable, "-m", "dbrain.outpath"] + [str(a) for a in args],
                          cwd=str(cwd or HERE), env=dict(os.environ, PYTHONPATH=str(HERE)),
                          capture_output=True, text=True)


class ExecLoggedTest(unittest.TestCase):
    """The launcher opens every log through `exec-logged`: checked and opened on one
    descriptor chain, so no path change between check and open can move it into git."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="dbrain-exec-logged-"))

    def test_the_command_runs_with_its_output_in_the_log(self):
        log = self.base / "logs" / "x.log"
        out = outpath_cli("exec-logged", log, "sh", "-c", "echo out; echo err >&2")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(sorted(log.read_text().split()), ["err", "out"])

    def test_a_restart_in_the_same_second_appends_never_truncates(self):
        log = self.base / "logs" / "x.log"
        outpath_cli("exec-logged", log, "echo", "first")
        outpath_cli("exec-logged", log, "echo", "second")
        self.assertEqual(log.read_text().split(), ["first", "second"])

    def test_a_log_inside_a_git_working_tree_is_refused_and_the_command_never_runs(self):
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        marker = self.base / "ran"
        for log in (repo / "x.log", repo / "artifacts" / "logs" / "x.log"):
            with self.subTest(log=str(log)):
                out = outpath_cli("exec-logged", log, "touch", marker)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("REFUSED", out.stderr)
                self.assertFalse(log.exists())
                self.assertFalse(marker.exists())

    def test_a_log_folder_that_is_a_symlink_into_git_is_refused(self):
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        (self.base / "logs").symlink_to(repo)
        out = outpath_cli("exec-logged", self.base / "logs" / "x.log", "echo", "hi")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(sorted(p.name for p in repo.iterdir()), [".git"])

    def test_a_git_entry_added_to_the_folder_later_is_refused_at_open(self):
        logs = self.base / "logs"
        self.assertEqual(outpath_cli("logdir", logs).returncode, 0)
        (self.base / ".git").write_text("gitdir: /elsewhere\n")    # the folder joins a repo
        out = outpath_cli("exec-logged", logs / "x.log", "echo", "hi")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((logs / "x.log").exists())

    def test_logdir_refuses_git_and_prints_the_real_path_otherwise(self):
        repo = self.base / "repo"
        (repo / ".git").mkdir(parents=True)
        self.assertNotEqual(outpath_cli("logdir", repo / "logs").returncode, 0)
        self.assertFalse((repo / "logs").exists())
        out = outpath_cli("logdir", self.base / "logs")
        self.assertEqual(out.stdout.strip(), str((self.base / "logs").resolve()))


class LauncherTest(unittest.TestCase):
    def test_every_log_the_launcher_writes_goes_through_exec_logged(self):
        text = (HERE / "mvp" / "run-all.sh").read_text()
        self.assertNotIn("artifacts/logs", text)
        self.assertIsNone(re.search(r">>?\s*\"?\$LOGS", text), "a shell redirect writes a log")
        self.assertIn("python3 -m dbrain.outpath logdir", text)
        self.assertIn("from dbrain import config; print(config.LOGS)", text)
        uses = [l for l in text.splitlines() if '"$LOGS/' in l]
        self.assertEqual(len(uses), 4)
        prev = text.splitlines()
        for line in uses:
            i = prev.index(line)
            self.assertTrue("logged" in line or "exec-logged" in line or "exec-logged" in prev[i - 1],
                            line)

    def service_env_lines(self):
        text = (HERE / "mvp" / "run-all.sh").read_text()
        start = text.index("# /ask service")
        return text[start:text.index("python3 -m dbrain.serve", start)]

    def test_the_service_gets_the_checked_absolute_log_folder(self):
        # A relative DBRAIN_LOGS would resolve against the service's own working folder
        # (C3b should-fix); the launcher passes back the real path logdir printed.
        self.assertIn('DBRAIN_LOGS="$LOGS"', self.service_env_lines())

    def test_capture_is_one_switch_passed_to_the_service(self):
        text = (HERE / "mvp" / "run-all.sh").read_text()
        self.assertEqual(len(re.findall(r"^CAPTURE=(on|off)$", text, re.M)), 1)
        self.assertIn('DBRAIN_FAILURE_CAPTURE="$CAPTURE"', self.service_env_lines())

    def test_a_relative_log_folder_comes_back_absolute(self):
        base = Path(tempfile.mkdtemp(prefix="dbrain-rel-logs-"))
        out = outpath_cli("logdir", "logs", cwd=base)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(Path(out.stdout.strip()).is_absolute())
        self.assertEqual(Path(out.stdout.strip()), (base / "logs").resolve())

    def test_the_launcher_parses(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(HERE / "mvp" / "run-all.sh")]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
