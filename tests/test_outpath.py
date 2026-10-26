import os
import tempfile
import unittest
from pathlib import Path

from dbrain import adapter, answer, config, outpath, trace
from tests import fake_vault


class WriteGuardTest(unittest.TestCase):
    """Nothing this service writes may land in the vault, however the path is supplied."""

    def setUp(self):
        self.vault = Path(config.VAULT)
        self.tmp = Path(tempfile.mkdtemp(prefix="dbrain-out-"))

    def test_a_path_outside_the_vault_is_allowed(self):
        self.assertTrue(outpath.assert_outside_vault(self.tmp / "report.json"))

    def test_a_path_inside_the_vault_is_refused(self):
        for rel in ("90 System/evil.jsonl",
                    "10 Projects/AI Second Brain/02_Architecture/results.json",
                    "index.md"):
            with self.assertRaises(outpath.OutsideVaultError, msg=rel):
                outpath.assert_outside_vault(self.vault / rel)

    def test_the_vault_root_itself_is_refused(self):
        with self.assertRaises(outpath.OutsideVaultError):
            outpath.assert_outside_vault(self.vault)

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_a_symlinked_directory_aimed_into_the_vault_is_refused(self):
        link = self.tmp / "sneaky"
        os.symlink(str(self.vault / "90 System"), str(link))
        with self.assertRaises(outpath.OutsideVaultError):
            outpath.assert_outside_vault(link / "evil.jsonl")

    def test_trace_refuses_an_artifacts_dir_inside_the_vault(self):
        vault, _, _ = fake_vault.build()
        result = answer.ask("What does tempo mean?", profile=fake_vault.PROFILE,
                            model=adapter.StubAdapter(reply="[E1]"), vault=vault)
        # config.VAULT is the real vault; aim the trace at it on purpose.
        with self.assertRaises(outpath.OutsideVaultError):
            trace.write(result, adapter.StubAdapter(), fake_vault.PROFILE,
                        artifacts=self.vault / "90 System")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_an_ancestor_swapped_for_a_symlink_after_the_check_is_refused(self):
        """Round C finding 2: the check passes, then a parent becomes a symlink into the
        vault before the open. The write must fail and the vault note stay intact."""
        vault = self.tmp / "vault"
        (vault / "notes" / "b").mkdir(parents=True)
        note = vault / "notes" / "b" / "out.json"
        note.write_text("precious")
        (self.tmp / "a" / "b").mkdir(parents=True)
        dest = self.tmp / "a" / "b" / "out.json"

        real_check = outpath.assert_outside_vault

        def check_then_swap(path, vault=None):
            target = real_check(path, vault)
            os.rename(str(self.tmp / "a"), str(self.tmp / "a-moved"))
            os.symlink(str(vault_dir / "notes"), str(self.tmp / "a"))
            return target

        vault_dir = vault
        outpath.assert_outside_vault = check_then_swap
        try:
            with self.assertRaises(outpath.OutsideVaultError):
                with outpath.open_for_write(dest, vault=vault) as fh:
                    fh.write("clobbered")
        finally:
            outpath.assert_outside_vault = real_check
        self.assertEqual(note.read_text(), "precious")

    def test_missing_parent_directories_are_created_by_the_guard(self):
        dest = self.tmp / "new" / "deeper" / "report.json"
        with outpath.open_for_write(dest) as fh:
            fh.write("{}")
        self.assertEqual(dest.read_text(), "{}")

    def _hard_linked_note(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        note = vault / "note.md"
        note.write_text("precious")
        link = self.tmp / "out" / "report.json"
        link.parent.mkdir()
        os.link(str(note), str(link))
        return vault, note, link

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_a_hard_link_to_a_vault_note_is_refused_and_not_truncated(self):
        """Round D finding 2: realpath and O_NOFOLLOW both pass a hard link."""
        vault, note, link = self._hard_linked_note()
        with self.assertRaises(outpath.OutsideVaultError):
            with outpath.open_for_write(link, vault=vault) as fh:
                fh.write("clobbered")
        self.assertEqual(note.read_text(), "precious")

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_a_hard_link_is_refused_in_append_mode_too(self):
        vault, note, link = self._hard_linked_note()
        with self.assertRaises(outpath.OutsideVaultError):
            with outpath.open_for_write(link, vault=vault, append=True) as fh:
                fh.write("appended")
        self.assertEqual(note.read_text(), "precious")

    def test_an_ordinary_existing_file_is_still_overwritten(self):
        dest = self.tmp / "report.json"
        dest.write_text("old and longer")
        with outpath.open_for_write(dest) as fh:
            fh.write("new")
        self.assertEqual(dest.read_text(), "new")

    def test_make_dir_refuses_the_vault_and_an_existing_directory(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        with self.assertRaises(outpath.OutsideVaultError):
            outpath.make_dir(vault / "evidence", vault=vault)
        self.assertFalse((vault / "evidence").exists())
        made = outpath.make_dir(self.tmp / "ev", vault=vault)
        self.assertTrue(made.is_dir())
        with self.assertRaises(outpath.OutsideVaultError):
            outpath.make_dir(self.tmp / "ev", vault=vault)


class WriteGuardRoundETest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbrain-oute-"))
        self.vault = self.tmp / "vault"
        self.vault.mkdir()

    def test_fresh_make_dir_refuses_anything_already_at_the_name(self):
        """Round E finding 1: existence check and creation are one atomic mkdir."""
        (self.tmp / "planted-dir").mkdir()
        os.symlink(str(self.vault), str(self.tmp / "planted-link"))
        for name in ("planted-dir", "planted-link"):
            with self.assertRaises(outpath.OutsideVaultError, msg=name):
                outpath.make_dir(self.tmp / name, vault=self.vault)
        self.assertEqual(list(self.vault.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_a_hard_link_made_after_open_is_reported_at_close(self):
        """Round E finding 2: detected, not prevented (threat model in outpath.py)."""
        dest = self.tmp / "out" / "report.json"
        fh = outpath.open_for_write(dest, vault=self.vault)
        fh.write("x")
        os.link(str(dest), str(self.vault / "linked.md"))
        with self.assertRaises(outpath.OutsideVaultError):
            fh.close()

class WriteGuardRoundFTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dbrain-outf-"))
        self.vault = self.tmp / "CaseVault"
        (self.vault / "notes").mkdir(parents=True)
        (self.vault / "index.md").write_text("precious")

    def test_a_case_alias_of_the_vault_is_refused(self):
        """Round F finding 1: on a case-insensitive volume `casevault` is the vault."""
        alias = self.tmp / "casevault"
        if not alias.exists():
            self.skipTest("case-sensitive filesystem: no alias to test")
        for dest in (alias / "index.md", alias / "notes" / "new.json", alias / "new" / "x.json"):
            with self.assertRaises(outpath.OutsideVaultError, msg=str(dest)):
                with outpath.open_for_write(dest, vault=self.vault) as fh:
                    fh.write("clobbered")
        with self.assertRaises(outpath.OutsideVaultError):
            outpath.make_dir(alias / "evidence", vault=self.vault)
        self.assertEqual((self.vault / "index.md").read_text(), "precious")
        self.assertFalse((self.vault / "evidence").exists())

    def test_fresh_make_dir_loses_a_creation_race_loudly(self):
        """Round F finding 4: someone creates the directory between our decision and our
        mkdir. The old check-then-create accepted it; the atomic mkdir must refuse."""
        real_mkdir = os.mkdir

        def racing(name, mode=0o777, *, dir_fd=None):
            real_mkdir(name, mode, dir_fd=dir_fd)        # the competitor wins
            return real_mkdir(name, mode, dir_fd=dir_fd)  # ours must now fail

        os.mkdir = racing
        try:
            with self.assertRaises(outpath.OutsideVaultError):
                outpath.make_dir(self.tmp / "raced", vault=self.vault)
        finally:
            os.mkdir = real_mkdir

if __name__ == "__main__":
    unittest.main()
