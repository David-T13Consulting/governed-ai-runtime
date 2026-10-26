"""P2.8 A1: the phone's profile leaves out the system's own working notes.

Every refusal is asserted on an attempted READ as well as on the file listing, as in
test_scope. All notes are synthetic and live in a temporary folder.
"""
import os
import shutil
import tempfile
import unicodedata
import sys
import unittest
from pathlib import Path

from dbrain import config, index, retrieve, scope

LABEL = "---\nclassification: internal\n---\n"
QUESTION = "is there climbing class on november 2?"

LIFE = {
    "00 Inbox/2026-10-05-1234-climbing.md":
        "# Climbing, Mondays\n\nMon 2 Nov: climbing class on, 16:30-17:30.\n",
    "20 Areas/Running/plan.md": "# Plan\n\nEasy run on Tuesday.\n",
    "40 Archive/Inbox/old.md": "# Old capture\n\nArchived life note.\n",
    "Welcome.md": "# Welcome\n\nObsidian's welcome note.\n",
    "90 Systems/near-miss.md": "# Near miss\n\nA folder whose name only starts the same.\n",
    "10 Projects/Healthcare Practice OS/README.md": "# Practice OS\n\nAnother project.\n",
}
# One note under every SYSTEM_NOTES entry, each quoting the test question, as the briefs do.
SYSTEM = {
    "90 System/briefs/MVP-phone.md": "# MVP-phone\n\n| Climbing on Mon 2 Nov? | is there climbing class on november 2 |\n",
    "90 System/handoff.md": "# Handoff\n\nDavid asked: is there climbing class on november 2?\n",
    "10 Projects/AI Second Brain/PROGRESS.md": "# Progress\n\nclimbing class november 2 regression\n",
    "10 Projects/AI Second Brain/02_Architecture/spec.md": "# Spec\n\nclimbing class on november 2\n",
    "40 Archive/agent-orchestration-proposal.md": "# Proposal\n\nclimbing class november 2\n",
    "AGENTS.md": "# Rules\n\nclimbing class november 2\n",
    "CLAUDE.md": "# Claude\n\nclimbing class november 2\n",
    "index.md": "# Index\n\nclimbing class november 2\n",
    "log.md": "# Log\n\n## [2026-10-14] climbing class on november 2\n",
}


def _write(root, notes):
    for rel, body in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(LABEL + body)


class PhoneCorpusTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="dbrain-p28-"))
        self.addCleanup(shutil.rmtree, self.vault, True)
        _write(self.vault, LIFE)
        _write(self.vault, SYSTEM)
        self.profile = config.MVP_WHOLE_VAULT

    def listed(self, profile=None):
        return {scope.as_posix(p) for p in scope.iter_allowed_files(profile or self.profile, self.vault)}

    def assert_unreadable(self, rel, profile=None):
        with self.assertRaises(scope.ScopeError, msg="%s was readable" % rel):
            scope.read_text(rel, profile or self.profile, self.vault)

    def test_every_system_notes_entry_is_covered_by_a_fixture(self):
        for entry in config.SYSTEM_NOTES:
            self.assertTrue(any(rel == entry or rel.startswith(entry + "/") for rel in SYSTEM),
                            "no fixture under %s" % entry)

    def test_phone_profile_excludes_the_system_notes(self):
        self.assertEqual(set(self.profile.excludes), set(config.SYSTEM_NOTES))
        for rel in ("90 System", "10 Projects/AI Second Brain", "log.md", "index.md",
                    "AGENTS.md", "CLAUDE.md"):
            self.assertIn(rel, config.SYSTEM_NOTES)

    def test_life_notes_are_listed_and_readable(self):
        self.assertEqual(self.listed(), set(LIFE))
        for rel in LIFE:
            self.assertIn("#", scope.read_text(rel, self.profile, self.vault))

    def test_system_notes_are_never_listed_or_read(self):
        listed = self.listed()
        for rel in SYSTEM:
            self.assertNotIn(rel, listed)
            self.assert_unreadable(rel)

    def test_excluded_folders_themselves_refuse_any_path_under_them(self):
        self.assert_unreadable("90 System/not-yet-written.md")
        self.assert_unreadable("10 Projects/AI Second Brain/deep/er/note.md")

    def test_case_and_unicode_variants_are_refused(self):
        # The Mac opens these as the excluded notes; the check must not be fooled.
        for rel in ("90 system/handoff.md", "90 SYSTEM/handoff.md", "agents.md", "LOG.MD",
                    "10 projects/ai second brain/PROGRESS.md"):
            self.assert_unreadable(rel)
        accented = config.Profile("t", roots=("",), excludes=("Réunions",))
        nfd = unicodedata.normalize("NFD", "Réunions") + "/x.md"
        nfc = unicodedata.normalize("NFC", "RÉUNIONS") + "/x.md"
        for rel in (nfd, nfc):
            with self.assertRaises(scope.ScopeError):
                scope.check_allowed(rel, accented, self.vault)

    @unittest.skipUnless(sys.platform == "darwin",
                         "macOS file systems treat NFC/NFD and case spellings as one name; "
                         "Linux does not, so the open-every-spelling half cannot hold there")
    def test_a_real_accented_folder_is_refused_by_every_spelling(self):
        # Review 1/5 should-fix: an actual file, read and listed, not only a path check.
        nfc = unicodedata.normalize("NFC", "Réunions")
        _write(self.vault, {nfc + "/minutes.md": "# Minutes\n\nACCENT_MARKER\n"})
        spellings = [unicodedata.normalize(f, n) + "/minutes.md"
                     for n in ("Réunions", "RÉUNIONS", "réunions") for f in ("NFC", "NFD")]
        open_ = config.Profile("open", roots=("",), skip_unlabelled=True)
        shut = config.Profile("shut", roots=("",), skip_unlabelled=True,
                              excludes=(unicodedata.normalize("NFD", "réunions"),))
        for rel in spellings:  # the Mac opens every one of these
            self.assertIn("ACCENT_MARKER", scope.read_text(rel, open_, self.vault))
            self.assert_unreadable(rel, shut)
        self.assertFalse([p for p in self.listed(shut) if "minutes" in p])
        _, files = index.build(shut, self.vault, skipped=[])
        self.assertFalse([p for p in files if "minutes" in p])

    def test_exclusion_compares_whole_components(self):
        self.assertIn("90 Systems/near-miss.md", self.listed())
        p = config.Profile("t", roots=("",), excludes=("log.md",))
        scope.check_allowed("log.md.bak/x.md", p, self.vault)  # not under log.md

    def test_exclusion_wins_over_a_named_file_and_a_root(self):
        p = config.Profile("t", roots=("90 System",), files=("AGENTS.md",),
                           excludes=("90 System", "AGENTS.md"))
        self.assertEqual(self.listed(p), set())
        self.assert_unreadable("AGENTS.md", p)
        self.assert_unreadable("90 System/handoff.md", p)

    def test_an_empty_exclusion_excludes_everything(self):
        p = config.Profile("t", roots=("",), excludes=("",))
        self.assertEqual(self.listed(p), set())
        self.assert_unreadable("Welcome.md", p)

    def test_other_profiles_are_unchanged(self):
        self.assertEqual(config.DEFAULT.excludes, ())
        self.assertEqual(config.DEVCASES.excludes, ())

    def test_index_never_holds_a_system_note(self):
        chunks, files = index.build(self.profile, self.vault, skipped=[])
        self.assertEqual(set(files), set(LIFE))
        self.assertFalse({c.path for c in chunks} & set(SYSTEM))

    def test_question_quoted_in_system_notes_retrieves_only_the_life_note(self):
        chunks, _ = index.build(self.profile, self.vault, skipped=[])
        hits = retrieve.Index(chunks).search(QUESTION, limit=8)
        self.assertTrue(hits)
        self.assertEqual(hits[0][1].path, "00 Inbox/2026-10-05-1234-climbing.md")
        self.assertFalse({c.path for _, c in hits} & set(SYSTEM))

    def test_without_the_exclusion_the_system_notes_win(self):
        # The 14 Oct failure, reproduced: proves the fixture would catch a regression.
        whole = config.Profile("w", roots=("",), exclude_hidden=True, skip_unlabelled=True)
        chunks, _ = index.build(whole, self.vault, skipped=[])
        hits = retrieve.Index(chunks).search(QUESTION, limit=8)
        self.assertIn(hits[0][1].path, SYSTEM)


if __name__ == "__main__":
    unittest.main()
