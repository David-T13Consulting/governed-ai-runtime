"""P2.5 slice F0: the regression-case file and its runner (experiments/p25_regression.py).

No model: the scorer is tested on fixed answers, by the classes of the D74 consultation of
9 Oct (session 45): valid answers, vocabulary versus absence, contradictions across every
separator, dates in every case type, activity and detail binding, whole-input coverage,
status, and reporting; every adversarial string an earlier review found is kept. The runner
runs end to end with the stub.
"""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from datetime import timedelta

from dbrain import adapter

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("p25_regression",
                                               str(ROOT / "experiments" / "p25_regression.py"))
p25 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(p25)

DOC = p25.load_cases()
CASE = {c["id"]: c for c in DOC["cases"]}
A1, B1, C1, D1 = "a1-row-term-absent", "b1-no-row", "c1-negated-row", "d1-other-activity-row"


def outcome(cid, text, status="incomplete"):
    return p25.score(CASE[cid], status, text, DOC)["outcome"]


def write_json(doc):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(doc, fh)
    return fh.name


class CaseFileTest(unittest.TestCase):
    def setUp(self):
        self.doc = p25.load_cases()

    def test_every_type_is_covered_and_known(self):
        self.assertEqual({c["type"] for c in self.doc["cases"]}, set(p25.TYPES))
        self.assertEqual(len({c["id"] for c in self.doc["cases"]}), len(self.doc["cases"]))

    def test_notes_are_labelled_and_synthetic(self):
        # Synthetic wording only (F0): none of the words of the real failure or its note.
        text = json.dumps([self.doc["notes"], self.doc["cases"]], ensure_ascii=False).lower()
        for word in ("baladin", "scout", "child", "uniform", "david"):
            self.assertNotIn(word, text)
        for rel, note in self.doc["notes"].items():
            self.assertTrue(note.startswith("---\nclassification: internal\n---\n"), rel)

    def test_incomplete_cases_are_refused(self):
        acts = {"choir": {"aliases": ["rehearsal"], "groups": []},
                "pottery": {"aliases": ["class"], "groups": []}}
        bad = [{"id": "t", "type": "maybe", "question": "q", "activity": "choir", "date": "2026-10-10"},
               {"id": "e", "type": "none", "question": "q", "date": "2026-10-10"},
               {"id": "e", "type": "none", "question": "q", "activity": "choir"},
               {"id": "r", "type": "row", "question": "q", "activity": "choir", "date": "2026-10-10",
                "absent_terms": ["y"]},
               {"id": "r", "type": "row", "question": "q", "activity": "choir", "date": "2026-10-10",
                "expect_start": "10:00"},
               {"id": "x", "type": "none", "question": "q", "activity": "choir", "date": "2026-10-10",
                "asked_terms": ["class"]}]
        for c in bad:
            name = write_json({"today": "2026-10-09", "notes": {}, "activities": acts,
                               "facts": [], "cases": [c]})
            try:
                with self.assertRaises(SystemExit, msg=c):
                    p25.load_cases(name)
            finally:
                os.unlink(name)

    def test_the_facts_are_the_notes_rows(self):
        # Ground truth is written out, and must agree with the synthetic notes themselves.
        for f in self.doc["facts"]:
            d = p25.date.fromisoformat(f["date"])
            day = "%s %d %s" % (d.strftime("%a"), d.day, d.strftime("%b"))
            notes = "\n".join(self.doc["notes"].values())
            line = [l for l in notes.splitlines() if l.startswith("| %s | %s |" % (day, f["event"]))]
            self.assertEqual(len(line), 1, f)
            if "start" in f:
                self.assertIn(f["start"], line[0])
        self.assertEqual(len(self.doc["facts"]), 18)

    def test_every_case_has_its_truth(self):
        for c in self.doc["cases"]:
            facts = [f for f in self.doc["facts"]
                     if f["activity"] == c["activity"] and f["date"] == c["date"]]
            if c["type"] == "row":
                self.assertEqual([f.get("start") for f in facts], [c["expect_start"]], c["id"])
            elif c["type"] == "negated":
                self.assertEqual([f.get("none") for f in facts], [True], c["id"])
            else:
                self.assertEqual(facts, [], c["id"])
            if c["type"] == "other":
                self.assertTrue(any(f["date"] == c["date"] for f in self.doc["facts"]), c["id"])


class ValidAnswerTest(unittest.TestCase):
    """Positive coverage: every claim form, EN and FR, all four types."""

    def test_row_answers_that_pass(self):
        for text in (
                "(Asked about Sat 10 Oct 2026.) Sat 10 Oct 2026: The note doesn't use the word "
                "cantata, but there is a normal rehearsal scheduled for 10:00-12:00 [1].",
                "The note doesn't use that word, but on Sat 10 Oct it lists a normal rehearsal, "
                "10:00-12:00 [1].",
                "No mention of cantata in the note; the normal rehearsal is scheduled from 10:00 "
                "to 12:00 on Sat 10 Oct 2026 [E1, E3].",
                "The calendar does not mention \u201ccantata practice\u201d. There is a rehearsal at "
                "10:00 [1]. There is also a normal outing scheduled for 14:00-17:30 [2].",
                "La note n'utilise pas le mot \u00abcantata\u00bb, mais il y a une répétition normale "
                "le samedi 10 octobre de 10:00 à 12:00.",
                "THE NOTE DOESN\u2019T USE THAT WORD. 2026-10-10: there is a rehearsal at 10h."):
            self.assertEqual(outcome(A1, text), "pass", text)

    def test_no_answers_that_pass(self):
        for cid in (B1, C1, D1):
            d = p25.date.fromisoformat(CASE[cid]["date"])
            day = "%s %d %s %d" % (d.strftime("%a"), d.day, d.strftime("%b"), d.year)
            for text in ("%s: Nothing found for %s." % (day, day),
                         "There is no choir rehearsal on this date [1].",
                         "The rehearsal is not scheduled that day.",
                         "Il n'y a pas de répétition ce jour-là.",
                         "Aucune répétition ce jour-là.",
                         "Il n'y a rien de prévu."):
                self.assertEqual(outcome(cid, text), "pass", (cid, text))

    def test_a_denial_may_give_the_notes_own_reason(self):
        for text in ('There is no choir rehearsal on this date because it is listed as '
                     '"No rehearsal (autumn break)" [1].',
                     "There is no choir rehearsal on this date because it is a no rehearsal due "
                     "to autumn break [1].",
                     "There is no rehearsal on Sat 17 Oct 2026 due to the autumn break.",
                     "There is no rehearsal on Sat 17 Oct because it is the autumn break."):
            self.assertEqual(outcome(C1, text), "pass", text)
        self.assertEqual(outcome("c3-negated-row", "There is no class on that date because it is "
                                                   "a holiday [1]."), "pass")
        # A reason where the notes give none, or in any other words: unread.
        self.assertEqual(outcome(B1, "There is no rehearsal on Sat 14 Nov because it is a break."),
                         "unscored")
        self.assertEqual(outcome(C1, "There is no rehearsal on Sat 17 Oct because the hall is shut."),
                         "unscored")

    def test_the_decline_text_only_where_the_notes_hold_nothing(self):
        text = ("(Asked about Sat 14 Nov 2026.) No supporting evidence in the p25-synthetic corpus, "
                "so I am not answering. Widen the allowlist or ask about something the notes cover.")
        self.assertEqual(outcome(B1, text, "withheld"), "pass")
        self.assertEqual(outcome(D1, text, "withheld"), "pass")
        self.assertEqual(outcome(C1, text, "withheld"), "fail")     # the note does say
        self.assertEqual(outcome(A1, text, "withheld"), "fail")
        self.assertEqual(outcome(B1, text, "incomplete"), "fail")

    def test_correct_paraphrases_outside_the_grammar_are_unscored_not_failed(self):
        for text in ("Yes! Choir is on, 10am.", "Sure, rehearsal as usual in the morning."):
            self.assertEqual(outcome(A1, text), "unscored", text)


class VocabularyVersusAbsenceTest(unittest.TestCase):
    def test_a_gap_alone_is_not_a_row_and_not_a_no(self):
        gap = "The note doesn't use that word."
        self.assertEqual(outcome(A1, gap), "fail")
        for cid in (B1, C1, D1):
            self.assertEqual(outcome(cid, gap), "fail", cid)

    def test_an_event_denial_never_passes_a_row_case(self):
        for text in ("The note doesn't use that word. There is no rehearsal on Sat 10 Oct.",
                     "Nothing found for Sat 10 Oct 2026.",
                     "The note doesn't use that word; there is a rehearsal at 10:00; nothing found."):
            self.assertEqual(outcome(A1, text), "fail", text)

    def test_the_row_without_the_gap_fails(self):
        self.assertEqual(outcome(A1, "Sat 10 Oct 2026: There is a normal rehearsal scheduled for "
                                     "10:00-12:00 [1]."), "fail")


class ContradictionTest(unittest.TestCase):
    GOOD = "The note doesn't use that word, but there is a rehearsal at 10:00 on Sat 10 Oct"
    NO = "there is no rehearsal on Sat 10 Oct"

    def test_a_valid_answer_with_an_opposing_claim_never_passes(self):
        for sep in (". ", "; ", ", but ", " but ", ", mais ", "\n"):
            for text in (self.GOOD + sep + self.NO + ".", self.NO + sep + self.GOOD + "."):
                self.assertNotEqual(outcome(A1, text), "pass", text)
                self.assertEqual(outcome(A1, text), "fail", text)

    def test_the_review_examples_verbatim(self):
        # Session 44 round 5 and session 45 round 1 (blocker 1).
        for text in ("The note does not use that word; No, only a normal rehearsal. "
                     "Sat 10 Oct 2026: 10:00.",
                     "The note does not use that word, but No, only a normal rehearsal. "
                     "Sat 10 Oct 2026: 10:00."):
            self.assertNotEqual(outcome(A1, text), "pass", text)

    def test_a_no_answer_with_a_row_fails(self):
        for text in ("Nothing found for Sat 31 Oct; there is an open studio day at 13:00.",
                     "There is no choir rehearsal on this date, but there is a night walk at 17:30."):
            self.assertEqual(outcome(D1, text), "fail", text)


class DateTest(unittest.TestCase):
    GAP = "The note doesn't use that word, but there is a rehearsal at 10:00 on "

    def test_row_dates(self):
        for when in ("Sat 10 Oct", "2026-10-10", "October 10", "10 octobre 2026", "Oct 10, 2026",
                     "this date"):
            self.assertEqual(outcome(A1, self.GAP + when + "."), "pass", when)
        for when in ("Sat 21 Nov", "2026-11-21", "November 21", "10 Oct 2027", "October 10, 2027",
                     "Sun 10 Oct", "31 Nov"):
            self.assertEqual(outcome(A1, self.GAP + when + "."), "fail", when)
        self.assertEqual(outcome(A1, "Sat 21 Nov 2026: " + self.GAP + "this date."), "fail")
        self.assertEqual(outcome(A1, self.GAP + "Sat 10 Oct, in 2027."), "unscored")

    def test_mixed_dates_fail(self):
        self.assertEqual(outcome(A1, self.GAP + "Sat 10 Oct. Nothing found for Sat 17 Oct."), "fail")

    def test_no_answers_must_name_the_asked_date(self):
        # Session 45 round 1, blocker 2.
        self.assertEqual(outcome(B1, "Nothing found for Sat 10 Oct 2026."), "fail")
        self.assertEqual(outcome(D1, "Nothing found for Sat 10 Oct 2026."), "fail")
        self.assertEqual(outcome(C1, "There is no choir rehearsal on Sat 10 Oct 2026."), "fail")
        self.assertEqual(outcome(B1, "Nothing found for Sat 14 Nov 2026."), "pass")
        self.assertEqual(outcome(C1, "There is no choir rehearsal on Sat 17 Oct 2026."), "pass")

    def test_the_service_prefix_supplies_and_hides_nothing(self):
        self.assertEqual(outcome(B1, "(Asked about Sat 14 Nov 2026.) Sat 10 Oct 2026: Nothing "
                                     "found for this date."), "fail")
        self.assertEqual(outcome(B1, "Nothing found. (Asked about Sat 10 Oct 2026.)"), "unscored")

    def test_an_unsupported_date_form_is_unscored(self):
        self.assertEqual(outcome(B1, "Nothing found for 14/11/2026."), "unscored")


class ActivityBindingTest(unittest.TestCase):
    def test_the_right_time_on_the_wrong_activity_fails(self):
        # Session 45 round 1, blocker 3.
        for text in ("The note does not use that word. Sat 10 Oct 2026: the pottery class is at 10:00.",
                     "The note does not use that word. There is a normal outing at 10:00 on Sat 10 Oct.",
                     "The note does not use that word. There is a pottery class at 19:00 on Sat 10 Oct."):
            self.assertEqual(outcome(A1, text), "fail", text)

    def test_the_target_only_in_the_gap_fails(self):
        self.assertEqual(outcome(A1, "The note doesn't use the word cantata. There is a normal "
                                     "outing scheduled for 14:00-17:30."), "fail")

    def test_an_unknown_activity_is_unscored(self):
        self.assertEqual(outcome(A1, "The note doesn't use that word. There is a knitting circle "
                                     "at 10:00."), "unscored")

    def test_the_event_must_be_the_one_on_that_date(self):
        # Session 45 round 2, blocker 2: the dinner is on 3 Oct at 18:30, not on 10 Oct.
        a5 = "a5-row-group-name-absent"
        self.assertEqual(outcome(a5, "The note does not use that word. There is a dinner at the club "
                                     "house at 14:00 on Sat 10 Oct."), "fail")
        self.assertEqual(outcome(a5, "The note does not use that word. There is a normal outing at "
                                     "14:00 on Sat 10 Oct."), "pass")
        self.assertEqual(outcome(a5, "The note does not use that word. There is a night walk at "
                                     "14:00 on Sat 10 Oct."), "fail")
        self.assertEqual(outcome("a2-row-term-absent", "The note does not use that word. There is a "
                                 "normal rehearsal at 09:00 on Sat 24 Oct."), "fail")

    def test_every_denial_is_checked_in_every_case_type(self):
        # Session 45 round 2, blocker 3: the night walk is on Sat 31 Oct.
        self.assertEqual(outcome(D1, "There is no choir rehearsal on Sat 31 Oct. There is no night "
                                     "walk on Sat 31 Oct."), "fail")
        self.assertEqual(outcome(D1, "There is no choir rehearsal on Sat 31 Oct. There is no pottery "
                                     "class on Sat 31 Oct."), "pass")     # the open studio is not a class
        self.assertEqual(outcome(B1, "Nothing found for Sat 14 Nov. There is no normal outing on "
                                     "this date."), "pass")
        self.assertEqual(outcome(C1, "There is no choir rehearsal on Sat 17 Oct. There is no outing "
                                     "on Sat 17 Oct."), "fail")

    def test_untimed_events_and_borrowed_reasons(self):
        # Session 45 round 3: the paper collection has no time; the autumn break is the choir's.
        self.assertEqual(outcome("d2-other-activity-row", "Nothing found for Tue 13 Oct. There is no "
                                 "paper on Tue 13 Oct."), "fail")
        self.assertNotEqual(outcome(C1, "There is no rehearsal on Sat 17 Oct. There is no pottery class "
                                        "on Sat 17 Oct because it is listed as \"No rehearsal (autumn "
                                        "break)\"."), "pass")
        self.assertEqual(outcome(C1, "There is no rehearsal on Sat 17 Oct because it is listed as "
                                     "\"No rehearsal (autumn break)\"."), "pass")

    def test_several_activities(self):
        both = ("The note doesn't use that word. There is a normal rehearsal at 10:00 [1]. "
                "There is also a normal outing scheduled for 14:00-17:30 [2].")
        self.assertEqual(outcome(A1, both), "pass")               # true extra row
        self.assertEqual(outcome(A1, both.replace("14:00", "11:00")), "fail")
        self.assertEqual(outcome(A1, "The note doesn't use that word. There is a rehearsal at 10:00. "
                                     "There is no normal outing on this date."), "fail")
        self.assertEqual(outcome(A1, "The note doesn't use that word. There is a normal rehearsal "
                                     "for the hiking club at 10:00."), "unscored")


class DetailBindingTest(unittest.TestCase):
    def test_times(self):
        gap = "The note doesn't use that word. "
        self.assertEqual(outcome(A1, gap + "There is a rehearsal at 11:00 on Sat 10 Oct."), "fail")
        self.assertEqual(outcome(A1, gap + "There is a rehearsal from 10:00 to 13:00."), "fail")
        self.assertEqual(outcome(A1, gap + "There is a rehearsal at 10:00. There is a rehearsal "
                                           "at 11:00."), "fail")
        self.assertEqual(outcome(A1, gap + "There is a rehearsal at 110:00."), "unscored")
        self.assertEqual(outcome(A1, gap + "There is a rehearsal at 10:00 or so."), "unscored")
        self.assertEqual(outcome(A1, gap + "There is a rehearsal at 25:00."), "fail")


class WholeInputTest(unittest.TestCase):
    GOOD = "The note doesn't use that word, but there is a rehearsal at 10:00 on Sat 10 Oct."

    def test_unknown_prose_anywhere_is_unscored(self):
        for text in (self.GOOD + " Hope that helps", "Well. " + self.GOOD,
                     'The note says "nothing found". ' + self.GOOD,
                     "There might be a rehearsal at 10:00. The note doesn't use that word.",
                     "The note doesn't use that word and there is a rehearsal at 10:00."):
            self.assertEqual(outcome(A1, text), "unscored", text)

    def test_a_denial_reason_cannot_turn_the_answer(self):
        # Session 45 round 2, blocker 1: a free reason tail carried a contrary claim.
        self.assertNotEqual(outcome(C1, "There is no choir rehearsal on this date because there "
                                        "is a choir rehearsal."), "pass")
        for tail in ("but it moved to 10:00", "though it was moved to Sunday", "it is in november",
                     "the 10 o'clock slot is gone", "it is listed as a normal rehearsal",
                     "it is a no rehearsal due to autumn break, there is a rehearsal"):
            self.assertNotEqual(outcome(C1, "There is no choir rehearsal on this date because "
                                            + tail + "."), "pass", tail)


class StatusTest(unittest.TestCase):
    def test_status(self):
        good = "The note doesn't use that word, but there is a rehearsal at 10:00."
        for status in ("withheld", "unclear", "malformed", "unavailable", "timeout", "error", None):
            self.assertEqual(outcome(A1, good, status), "fail", status)
        for status in ("unclear", "malformed", "timeout"):
            self.assertEqual(outcome(B1, "Nothing found for Sat 14 Nov.", status), "fail", status)
        self.assertEqual(outcome(A1, ""), "fail")
        self.assertEqual(outcome(B1, ""), "fail")
        self.assertEqual(outcome(B1, "No supporting evidence in the p25-synthetic corpus, so I am "
                                     "not answering. There is a rehearsal at 10:00.", "withheld"), "fail")


class GeneratedAnswerTest(unittest.TestCase):
    """Answers built from claim templates whose meaning is known in advance, combined up to
    three at a time for every case: the scorer may pass one only if an oracle computed from
    the facts (not from the text) says it meets the case's contract."""

    def library(self, case):
        asked = p25.date.fromisoformat(case["date"])
        other = asked + timedelta(days=7)
        fmt = lambda d: "%s %d %s %d" % (d.strftime("%a"), d.day, d.strftime("%b"), d.year)
        acts = DOC["activities"]
        out = [("The note doesn't use that word", {"kind": "gap"})]
        for act, spec in acts.items():
            for alias in (spec["aliases"][0], *spec.get("specific", {}))[:3]:
                for d in (asked, other):
                    for start in ("10:00", "14:00", "09:00", "19:00"):
                        out.append(("There is a %s at %s on %s" % (alias, start, fmt(d)),
                                    {"kind": "pos", "act": act, "alias": alias, "date": d, "start": start}))
                    out.append(("There is no %s on %s" % (alias, fmt(d)),
                                {"kind": "den", "act": act, "alias": alias, "date": d}))
        for d in (asked, other):
            out.append(("Nothing found for %s" % fmt(d), {"kind": "den", "act": None, "alias": None, "date": d}))
        return out

    def names(self, act, alias, fact):
        spec = DOC["activities"][act].get("specific", {})
        return alias not in spec or spec[alias] == fact["event"]

    def oracle(self, case, claims):
        asked = p25.date.fromisoformat(case["date"])
        facts = lambda act, d: [f for f in DOC["facts"] if f["activity"] == act and f["date"] == d.isoformat()]
        if any(c.get("date", asked) != asked for c in claims):
            return False
        for c in claims:
            if c["kind"] == "pos" and not any(f.get("start") == c["start"] and self.names(c["act"], c["alias"], f)
                                              for f in facts(c["act"], c["date"])):
                return False
            if c["kind"] == "den":
                act = c["act"] or case["activity"]
                if any(not f.get("none") and self.names(act, c["alias"], f) for f in facts(act, c["date"])):
                    return False
        mine_den = [c for c in claims if c["kind"] == "den" and c["act"] in (None, case["activity"])]
        pos = [c for c in claims if c["kind"] == "pos"]
        if case["type"] == "row":
            return (any(c["kind"] == "gap" for c in claims) and not mine_den
                    and any(c["act"] == case["activity"] and c["start"] == case["expect_start"] for c in pos))
        return bool(mine_den) and not pos

    def test_no_generated_answer_passes_wrongly(self):
        import itertools
        checked = 0
        for case in DOC["cases"]:
            lib = self.library(case)
            combos = [(x,) for x in lib] + list(itertools.combinations(lib, 2))
            combos += list(itertools.combinations(lib[::7], 3))
            for combo in combos:
                text = ". ".join(t for t, _ in combo) + "."
                got = p25.score(case, "incomplete", text, DOC)["outcome"]
                want = self.oracle(case, [m for _, m in combo])
                checked += 1
                # Every template is a supported form: an unread one would make this vacuous.
                self.assertNotEqual(got, "unscored", (case["id"], text))
                if got == "pass":
                    self.assertTrue(want, (case["id"], text))
                elif got == "fail":
                    self.assertFalse(want, (case["id"], text))
        self.assertGreater(checked, 10000)


class ReportTest(unittest.TestCase):
    def rows(self):
        return [{"id": A1, "type": "row", "status": "incomplete", "answer": "x",
                 "answer_sha256": p25.answer_sha256("incomplete", "x"),
                 "score": {"outcome": "unscored", "reasons": []}},
                {"id": B1, "type": "none", "status": "incomplete", "answer": "y",
                 "answer_sha256": p25.answer_sha256("incomplete", "y"),
                 "score": {"outcome": "pass", "reasons": []}}]

    def test_unscored_stays_in_the_count(self):
        bt = p25.by_type(self.rows())
        self.assertEqual(bt["row"], {"pass": 0, "fail": 0, "unscored": 1, "cases": 1})

    def test_a_read_applies_only_to_the_answer_it_was_made_on(self):
        path = write_json({"reads": [{"case": A1, "answer_sha256": p25.answer_sha256("incomplete", "x"),
                                      "verdict": "fail", "by": "lead", "why": "t"},
                                     {"case": B1, "answer_sha256": p25.answer_sha256("incomplete", "old"),
                                      "verdict": "pass", "by": "lead", "why": "t"}]})
        try:
            report = p25.adjudicate({"cases": self.rows()}, path)
        finally:
            os.unlink(path)
        self.assertEqual(report["cases"][0]["read"]["verdict"], "fail")
        self.assertNotIn("read", report["cases"][1])
        self.assertEqual(report["needs_read"], [B1])

    def test_without_any_reads_every_answer_needs_one(self):
        # Session 45 round 2, should-fix: a missing reads file listed nothing.
        report = p25.adjudicate({"cases": self.rows()}, "/nonexistent/p25_adjudication.json")
        self.assertEqual(report["needs_read"], [A1, B1])


class VaultPathTest(unittest.TestCase):
    def test_note_paths_cannot_leave_the_folder(self):
        # Codex round 3: an absolute path or ".." reached write_text.
        for rel in ("/tmp/x.md", "../x.md", "a/../../x.md", "a/./x.md", ".hidden/x.md",
                    "a//x.md", "x.txt", " a/x.md", "a /x.md", "~/x.md"):
            with self.assertRaises(SystemExit, msg=rel):
                p25.build_vault({rel: "---\nclassification: internal\n---\n"})

    def test_the_folder_ignores_tmpdir_and_is_guarded(self):
        # Codex round 4: mkdtemp trusted TMPDIR, which could point into the vault.
        old = os.environ.get("TMPDIR")
        os.environ["TMPDIR"] = str(p25.config.VAULT)
        try:
            root = p25.build_vault({"20 Areas/Hobbies/a.md": "x"})
        finally:
            if old is None:
                del os.environ["TMPDIR"]
            else:
                os.environ["TMPDIR"] = old
        try:
            self.assertEqual(root.parent, p25.TMP_PARENT)
            self.assertTrue(root.name.startswith(p25.TMP_PREFIX))
            self.assertEqual((root / "20 Areas/Hobbies/a.md").read_text(), "x")
        finally:
            p25.remove_vault(root)
        self.assertFalse(root.exists())

    def test_the_guard_refuses_a_parent_in_the_vault(self):
        real = p25.TMP_PARENT
        p25.TMP_PARENT = Path(str(p25.config.VAULT))
        try:
            with self.assertRaises(p25.outpath.OutsideVaultError):
                p25.build_vault({"a.md": "x"})
        finally:
            p25.TMP_PARENT = real

    def test_only_a_synthetic_vault_is_removed(self):
        for path in (p25.config.VAULT, Path("/private/tmp"), Path("/private/tmp/other"),
                     Path("/private/tmp/x/dbrain-p25-1")):
            with self.assertRaises(SystemExit, msg=str(path)):
                p25.remove_vault(path)

    def test_the_model_endpoint_must_be_named(self):
        # Never the live phone server by default (Codex round 3).
        with self.assertRaises(SystemExit):
            p25.main(["--label", "x"])


class RunTest(unittest.TestCase):
    def test_runner_end_to_end_with_the_stub(self):
        doc = p25.load_cases()
        made = []
        real = p25.build_vault

        def spy(notes):
            root = real(notes)
            made.append(root)
            return root

        p25.build_vault = spy
        try:
            report = p25.run(doc, adapter.StubAdapter())
        finally:
            p25.build_vault = real
        self.assertEqual(len(report["cases"]), len(doc["cases"]))
        self.assertEqual(set(report["by_type"]), set(p25.TYPES))
        self.assertTrue(all(r["score"]["outcome"] in p25.OUTCOMES for r in report["cases"]))
        self.assertFalse(made[0].exists())          # the synthetic vault is removed

    def test_compare_names_changed_cases(self):
        old = {"cases": [{"id": "a", "status": "incomplete", "answer": "x"},
                         {"id": "b", "status": "withheld", "answer": "y"}]}
        new = {"cases": [{"id": "a", "status": "incomplete", "answer": "x"},
                         {"id": "b", "status": "incomplete", "answer": "z"},
                         {"id": "c", "status": "incomplete", "answer": "w"}]}
        self.assertEqual(p25.compare(old, new),
                         ["cases b: withheld -> incomplete", "cases c: new"])


if __name__ == "__main__":
    unittest.main()
