import os
import unittest
from pathlib import Path

from dbrain import index, scope
from tests import fake_vault

NEEDS_LINKS = unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")


class ScopeTest(unittest.TestCase):
    """Every refusal is asserted on an attempted READ, not only on a path check: a
    rejection by `resolve` alone would not prove the file is unreachable."""

    def setUp(self):
        self.vault, self.outside, self.links = fake_vault.build()
        self.profile = fake_vault.PROFILE

    def read(self, rel):
        return scope.read_text(rel, self.profile, self.vault)

    def assert_unreadable(self, rel):
        with self.assertRaises(scope.ScopeError, msg="%s was readable" % rel):
            self.read(rel)

    # --- the allowed case, so the suite is not vacuously green ---
    def test_allowed_file_is_readable(self):
        self.assertIn("LTHR", self.read("20 Areas/Running/athlete-baseline.md"))

    # --- plain refusals ---
    def test_file_in_vault_but_outside_allowlist_is_unreadable(self):
        self.assert_unreadable("90 System/secrets.md")

    def test_vault_root_file_is_unreadable(self):
        self.assert_unreadable("AGENTS.md")

    def test_dotdot_escape_is_unreadable(self):
        self.assert_unreadable("20 Areas/Running/../../90 System/secrets.md")
        self.assert_unreadable("20 Areas/Running/../../../etc/hosts")

    def test_absolute_path_outside_the_vault_is_unreadable(self):
        self.assert_unreadable(str(self.outside / "source.md"))
        self.assert_unreadable("/etc/hosts")

    def test_home_source_material_is_unreadable(self):
        self.assert_unreadable(str(Path.home() / "Documents" / "Source_Material" / "x.md"))

    # --- symlinks: no link anywhere on the path may be followed ---
    @NEEDS_LINKS
    def test_symlink_in_allowed_root_pointing_out_of_vault_is_unreadable(self):
        self.assert_unreadable("20 Areas/Running/leak.md")

    @NEEDS_LINKS
    def test_symlink_in_allowed_root_pointing_to_forbidden_note_is_unreadable(self):
        self.assert_unreadable("20 Areas/Running/inward.md")

    @NEEDS_LINKS
    def test_symlinked_allowlist_root_authorises_nothing(self):
        """`30 Resources/Local AI` is an allowlisted root that is itself a link into
        `90 System`. Resolving it would have authorised its target."""
        self.assert_unreadable("30 Resources/Local AI/secrets.md")
        indexed = scope.iter_allowed_files(self.profile, self.vault)
        for parts in indexed:
            self.assertNotIn("secrets.md", parts, "a symlinked root leaked its target")

    @NEEDS_LINKS
    def test_symlinked_allowlist_file_authorises_nothing(self):
        self.assert_unreadable("alias-AGENTS.md")

    @NEEDS_LINKS
    def test_symlinked_intermediate_directory_is_unreadable(self):
        self.assert_unreadable("30 Resources/Running/sub/deep.md")

    @NEEDS_LINKS
    def test_no_symlink_is_ever_indexed(self):
        for parts in scope.iter_allowed_files(self.profile, self.vault):
            p = self.vault.joinpath(*parts)
            cur = self.vault
            for part in parts:
                cur = cur / part
                self.assertFalse(cur.is_symlink(), "indexed through a symlink: %s" % p)

    @NEEDS_LINKS
    def test_a_swap_to_a_symlink_after_indexing_is_caught_at_read(self):
        """The check-then-open race: replace an indexed file with a symlink, then read.
        The anchored traversal refuses it rather than following it."""
        target = self.vault / "20 Areas/Running/athlete-baseline.md"
        target.unlink()
        os.symlink(str(self.outside / "source.md"), str(target))
        self.assert_unreadable("20 Areas/Running/athlete-baseline.md")

    # --- content never reaches the caller ---
    def test_forbidden_content_never_appears_in_an_index(self):
        chunks, files = index.build(self.profile, self.vault)
        blob = "\n".join(c.text for c in chunks)
        for marker in ("NOT_IN_ANY_ALLOWLIST", "VAULT_ROOT_FILE", "OUTSIDE_VAULT",
                       "DEEP_OUTSIDE"):
            self.assertNotIn(marker, blob, "%s leaked into the index" % marker)

    def test_case_mismatched_root_fails_closed(self):
        """macOS filesystems are usually case-insensitive; the allowlist compares path
        components, so a mis-cased root is refused. Over-strict, never under."""
        self.assert_unreadable("30 RESOURCES/Running/session-types.md")


class HardLinkReadTest(unittest.TestCase):
    """Round E finding 3: an allowlisted name hard-linked to a forbidden note."""

    @unittest.skipUnless(hasattr(os, "link"), "needs hard links")
    def test_a_hard_linked_allowlisted_file_is_refused(self):
        vault, _, _ = fake_vault.build()
        alias = vault / "20 Areas/Running/innocent.md"
        os.link(str(vault / "90 System/secrets.md"), str(alias))
        with self.assertRaises(scope.ScopeError):
            scope.read_text("20 Areas/Running/innocent.md", fake_vault.PROFILE, vault)
        with self.assertRaises(scope.ScopeError):
            index.build(fake_vault.PROFILE, vault)

if __name__ == "__main__":
    unittest.main()
