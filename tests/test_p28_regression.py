"""P2.8 A2: the 14 Oct phone failure as a regression case, on synthetic notes.

On 14 Oct the phone was asked whether a Monday class was on, on Mon 2 Nov, and cited a
project brief that quoted the question instead of the note that lists the day. The real
note stays out of the test suite (P2.5 F0 rule: synthetic wording only); the activity here
is pottery. The decoys quote each question word for word, as the project's briefs, PROGRESS
and log did. The model is a stub: this checks what the phone path retrieves and cites. The
model's own wording is checked on the phone with David (brief, step A2).
"""
import datetime
import shutil
import tempfile
import unittest
from pathlib import Path

from dbrain import adapter, config, serve

TOKEN = "t" * 40  # test-only placeholder
TODAY = datetime.date(2026, 10, 14)
LABEL = "---\nclassification: internal\n---\n"
LIFE = "00 Inbox/2026-10-05-1200-pottery-mondays-2026-27.md"
QUESTIONS = (
    "is there pottery class on november 2?",
    "pottery on Mon 2 Nov?",
)
# Retrieval is lexical: a French question shares no word with an English note, so it
# finds nothing (known gap, recorded in the brief). What must hold is that it never falls
# back to a system note quoting it.
FRENCH = "poterie le 2 novembre ?"
DECOYS = (
    "90 System/briefs/MVP-phone.md",
    "90 System/briefs/P2.8-phone-corpus-and-answer-screen.md",
    "10 Projects/AI Second Brain/PROGRESS.md",
    "log.md",
    "index.md",
)


def build_vault():
    root = Path(tempfile.mkdtemp(prefix="dbrain-p28a2-"))
    note = root / LIFE
    note.parent.mkdir(parents=True)
    note.write_text(
        LABEL + "# Pottery, Mondays 2026-27\n\n## Autumn term\n\n"
        "| Date | Time | Status |\n|---|---|---|\n"
        "| Mon 26 Oct | — | no class (holiday) |\n"
        "| Mon 2 Nov | 16:30–17:30 | class on |\n"
        "| Mon 9 Nov | 16:30–17:30 | class on |\n")
    quoted = "\n".join("| %s | Mon 2 Nov 2026 november 2 novembre | DECOY_MARKER |" % q
                       for q in QUESTIONS + (FRENCH,))
    for rel in DECOYS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(LABEL + "# Test log\n\n| Question | Date | Result |\n|---|---|---|\n"
                     + (quoted + "\n") * 3)
    return root


class PhoneRegressionTest(unittest.TestCase):
    def setUp(self):
        self.vault = build_vault()
        self.addCleanup(shutil.rmtree, self.vault, True)

    def ask(self, question):
        model = adapter.StubAdapter()
        svc = serve.Service(TOKEN, model_factory=lambda: model, vault=self.vault,
                            tracing=False, today=lambda: TODAY)
        self.assertIs(svc.profile, config.MVP_WHOLE_VAULT)  # the phone's own profile
        code, payload = svc.ask(question)
        return code, payload, model

    def test_each_form_cites_only_the_life_note(self):
        for q in QUESTIONS:
            with self.subTest(q=q):
                code, payload, model = self.ask(q)
                self.assertEqual(code, 200)
                self.assertTrue(payload["citations"], payload)
                self.assertEqual({c["path"] for c in payload["citations"]}, {LIFE})
                self.assertNotIn("DECOY_MARKER", "".join(model.calls))
                self.assertIn("16:30", "".join(model.calls))  # the day's row reached the model

    def test_the_french_form_never_cites_a_system_note(self):
        code, payload, model = self.ask(FRENCH)
        self.assertEqual(code, 200)
        self.assertNotIn("DECOY_MARKER", "".join(model.calls))
        self.assertFalse({c["path"] for c in payload["citations"]} & set(DECOYS))
        self.assertEqual(payload["status"], "withheld")

    def test_the_date_is_resolved_to_mon_2_nov(self):
        for q in QUESTIONS + (FRENCH,):
            with self.subTest(q=q):
                _, payload, _ = self.ask(q)
                self.assertIn("Mon 2 Nov 2026", payload["answer"])

    def test_the_decoys_would_win_without_the_exclusion(self):
        # Proves the fixture reproduces the failure: same notes, the old profile.
        old = config.Profile("old-whole-vault", roots=("",), exclude_hidden=True,
                             skip_unlabelled=True)
        model = adapter.StubAdapter()
        svc = serve.Service(TOKEN, model_factory=lambda: model, profile=old,
                            vault=self.vault, tracing=False, today=lambda: TODAY)
        _, payload = svc.ask(QUESTIONS[0])
        self.assertTrue({c["path"] for c in payload["citations"]} & set(DECOYS))


if __name__ == "__main__":
    unittest.main()
