import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

from dbrain import adapter, answer, config, scope

REPO = Path(__file__).resolve().parent.parent


def snapshot_vault(root):
    """Hash and stat every file in the vault, not only the allowlisted ones.

    Checking only allowed files would miss a write to `90 System/` or to a note the
    profile excludes - exactly the kind of escape the read-only claim is about. `.git`
    is skipped: git's own housekeeping touches it independently of this service.
    """
    out = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
                out[str(p)] = (st.st_mtime_ns, st.st_size,
                               hashlib.sha256(p.read_bytes()).hexdigest())
            except (OSError, ValueError):
                out[str(p)] = "unreadable"
    return out


class ReadOnlyTest(unittest.TestCase):
    def test_an_in_process_query_changes_nothing_in_the_whole_vault(self):
        root = Path(config.VAULT)
        before = snapshot_vault(root)
        self.assertGreaterEqual(len(before), 8, "vault snapshot too small to prove anything")
        result = answer.ask("What counts as a tempo session?", profile=config.DEFAULT,
                            model=adapter.StubAdapter(reply="[E1] tempo marathon pace"))
        self.assertTrue(result.status)
        self.assertEqual(before, snapshot_vault(root), "the vault changed during a query")

    def test_the_cli_including_tracing_changes_nothing_in_the_whole_vault(self):
        """The in-process test skips the trace writer. This runs the real entry point
        with tracing enabled, which is the path that actually writes something."""
        root = Path(config.VAULT)
        before = snapshot_vault(root)
        proc = subprocess.run(
            [sys.executable, "query", "What counts as a tempo session?", "--stub-model"],
            cwd=str(REPO), capture_output=True, text=True,
        )
        self.assertIn(proc.returncode, (0, 2), proc.stderr)
        self.assertEqual(before, snapshot_vault(root),
                         "the vault changed during a traced CLI query")

    def test_scope_module_opens_read_only_and_offers_no_writer(self):
        # Strip comments and docstring lines: prose naming a flag is not a use of it.
        raw = Path(scope.__file__).read_text()
        src = "\n".join(
            line.split("#", 1)[0] for line in raw.splitlines()
            if not line.strip().startswith(('"""', "'''", "#"))
        )
        for forbidden in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"):
            self.assertNotIn(forbidden, src, "scope.py references %s" % forbidden)
        self.assertIn("os.O_RDONLY", src)
        for name in dir(scope):
            self.assertNotIn(name, ("write_text", "write", "unlink", "mkdir", "rmtree"))

    def test_on_a_synthetic_vault_ask_writes_nothing_and_a_capture_one_inbox_file(self):
        """PL1 step 6: the one write the service can make is one new file in 00 Inbox/,
        and only on save. A synthetic vault, never the real one."""
        from datetime import datetime
        from dbrain import serve
        from tests.test_serve import TOKEN, whole_vault_fixture
        root = whole_vault_fixture()
        svc = serve.Service(TOKEN, model_factory=lambda: adapter.StubAdapter(reply="[E1] tempo"),
                            vault=root, tracing=False)
        svc.now = lambda: datetime(2026, 10, 22, 9, 30)
        before = snapshot_vault(root)
        svc.ask("What does tempo mean?")
        status, _, fields = svc.draft_capture("Tempo pacing idea for the router talk", "work")
        self.assertEqual(status, "draft")
        self.assertEqual(before, snapshot_vault(root), "an answer or a draft changed the vault")
        outcome, path = svc.save_capture(fields["id"])
        self.assertEqual(outcome, "saved")
        after = snapshot_vault(root)
        added = set(after) - set(before)
        self.assertEqual(added, {str(root / path)})
        self.assertTrue(path.startswith("00 Inbox/"))
        self.assertEqual({k: v for k, v in after.items() if k not in added}, before)

    def test_traces_default_outside_the_vault(self):
        self.assertFalse(
            str(Path(config.LOGS).resolve()).startswith(
                str(Path(config.VAULT).resolve()) + os.sep),
            "the default trace directory is inside the vault")


if __name__ == "__main__":
    unittest.main()
