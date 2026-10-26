"""P2.8 B1: the phone's answer screen (`serve.display_text`).

The screen's promise (brief P2.8, Done when): no table pipe, no `**`, no evidence id and
no `.md` path; the answer first, one line per source by title, one status line, latency
only past 8 s. The JSON keeps every field it had. Test list from the D129 design consult
(Codex, 14 Oct): whole-display invariants, clean-up adversaries, preservation of the
service's own sentences, robustness, integration.
"""
import copy
import datetime
import random
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from dbrain import adapter, answer, index, serve

TOKEN = "t" * 40  # test-only placeholder
TODAY = datetime.date(2026, 10, 14)
LABEL = "---\nclassification: internal\n---\n"
# Literal substrings, glued or not: ".mdx" and ".md_backup" count (B1 D74 consult).
FORBIDDEN = re.compile(r"\||\*\*|\[E|\.md", re.I)

ADVERSARIES = (
    "**Yes.** The class is on [E1].",
    "| Date | Time |\n|---|---|\n| Mon 2 Nov | 16:30 |",
    "Mon 2 Nov: on [E1, E4]. See `90 System/briefs/MVP-phone.md:67-84`.",
    "On [E1;E2] and [ E3 ] and [e5] and [E 6].",
    "*`*bold*`* and [E`2] and note.`md and _`_x_`_",
    "# Heading\r\n- bullet one\r\n* bullet two\r\n1. numbered",
    "Path: 00 Inbox/2026-10-05-1234-x.md, also notes/a.MD:3-9.",
    "Pipes | in | prose || and ***triple***",
    "[E" + "9" * 400 + "] huge id",
    "",
    "\n\n\n",
    "Read notes.md_backup and notes.mdx and x.MD5.",
    "Thursday\u2028Cancelled\u2029*\u200b*[\u200bE1] .\u200bmd",
)


def payload(answer_text, cites=(), status="incomplete", status_line="Not verified",
            latency=1.0):
    return {"status": status, "reason": "r", "answer": answer_text,
            "citations": [dict(c) for c in cites], "latency_s": latency,
            "status_line": status_line}


class PhoneTextTest(unittest.TestCase):
    def test_no_adversary_leaves_a_forbidden_token(self):
        for text in ADVERSARIES:
            with self.subTest(text=text[:40]):
                out = serve.display_text(payload(text, [{"path": "a/b.md", "quote": [text]}]))
                self.assertIsNone(FORBIDDEN.search(out), out)

    def test_random_strings_never_crash_or_leak(self):
        rng = random.Random(14)
        alphabet = "|*_`[]E0123456789 .md/\n\r-#:,;“”()abc\u2028\u200b\u00ad"
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
            out = serve.display_text(payload(text, [{"path": text, "quote": [text]}]), [text])
            self.assertIsNone(FORBIDDEN.search(out), repr(text))
            asked = "(Asked about %s.) %s" % (text.replace("(", "").replace(")", ""), text)
            out = serve.display_text(payload(asked))
            self.assertIsNone(FORBIDDEN.search(out), repr(asked))

    def test_review_1_reproductions(self):
        # B1 review 1/2 (Codex, 14 Oct): each blocker's own input.
        label = payload("(Asked about **Mon 2 Nov** | notes/x.md.) The class is on.")
        self.assertIsNone(FORBIDDEN.search(serve.display_text(label)))
        rebuilt = payload("Read notes/x.m*" + "[e1" * 10 + "]" * 10 + "*d.")
        self.assertIsNone(FORBIDDEN.search(serve.display_text(rebuilt)))
        glued = serve.display_text(payload("Yes.`notes/x.md` The class is on."))
        self.assertTrue(glued.startswith("Yes. notes/x The class is on."), glued)

    def test_the_guard_creates_nothing_it_forbids(self):
        for text in ("***", "*****", "[[E", "x.md.md", "|*|*|", "[ e1]", ".MD", "a.md:3-4"):
            out = serve._guard(serve.phone_text(text))
            self.assertIsNone(FORBIDDEN.search(out), (text, out))

    def test_table_rows_become_cells_and_bullets_lose_their_marks(self):
        out = serve.phone_text("| Mon 2 Nov | 16:30–17:30 | class on |\n|---|---|\n- one\n2. two")
        self.assertEqual(out, "Mon 2 Nov · 16:30–17:30 · class on one two")

    def test_renumbered_ids_stay_and_evidence_ids_go(self):
        out = serve.phone_text("On [1]. Also [E2] and [E1, E4].")
        self.assertEqual(out, "On [1]. Also and.")

    def test_every_sentence_of_the_service_survives(self):
        # The parked P2.5 F2 change appends this to an answer; it must reach the screen.
        appended = ("There is nothing on Mon 2 Nov [1].\n\nBut the note it cites lists this "
                    "for that day: “Mon 2 Nov · 16:30–17:30 · class on” [1]. Check the note.")
        out = serve.display_text(payload(appended, [{"path": "00 Inbox/x.md"}]))
        for part in ("There is nothing on Mon 2 Nov [1].",
                     "But the note it cites lists this for that day: “Mon 2 Nov · 16:30–17:30 "
                     "· class on” [1]. Check the note."):
            self.assertIn(part, out)


class LayoutTest(unittest.TestCase):
    CITES = [{"path": "00 Inbox/2026-10-05-1234-pottery-mondays.md", "unreviewed": True,
              "quote": ["| Mon 2 Nov | 16:30–17:30 | class on |", "second line"]},
             {"path": "20 Areas/Running/plan.md", "unreviewed": False, "quote": []}]

    def test_answer_then_sources_with_one_quote_then_status(self):
        out = serve.display_text(payload("The class is on [1] [2].", self.CITES,
                                         status_line="Not verified · from an unreviewed Inbox note"),
                                 ["Pottery, Mondays", "Plan"])
        self.assertEqual(out.splitlines(), [
            "The class is on [1] [2].", "",
            "[1] Pottery, Mondays", "    “Mon 2 Nov · 16:30–17:30 · class on”",
            "[2] Plan", serve.NO_QUOTE, "",
            "Not verified · from an unreviewed Inbox note"])

    def test_quotes_are_cleaned_never_cut(self):
        # B1 review 1 (session 50): " · " for each pipe lengthened a 144-character row past
        # the old display cut and hid its last cell. The JSON quote is cut once, raw, by
        # quote_lines; the screen shows all of it.
        row = ("|Mon 2 Nov|16:30-17:30|Pottery|Main hall|Beginner|Indoor|Apron|Water|Tools "
               "provided|Booking required|Adults only|Weekly|Arrival 16:15|CANCELLED|")
        cites = [{"path": "a.md", "quote": ["", "**x** `b.md` [E1] " + "y" * 300]},
                 {"path": "b.md", "quote": [row]}]
        out = serve.display_text(payload("On [1] [2].", cites), ["A", "B"])
        lines = out.splitlines()
        self.assertEqual(lines[3], "    “x b " + "y" * 300 + "”")
        self.assertTrue(lines[5].endswith("· Arrival 16:15 · CANCELLED”"), lines[5])
        self.assertIsNone(FORBIDDEN.search(out), out)

    def test_a_long_screen_line_is_cut_raw_like_the_json_quote(self):
        row = "|" + "|".join("cell%d" % i for i in range(60)) + "|CANCELLED|"
        shown = serve.screen_line(row, "x")
        self.assertEqual(shown, serve.phone_text(serve._cut(row, serve.QUOTE_WIDTH)))

    def test_the_resolved_day_leads_the_answer_once(self):
        with_day = payload("(Asked about Mon 2 Nov 2026.) Mon 2 Nov 2026: on [1].")
        without = payload("(Asked about Mon 2 Nov 2026.) The class is on [1].")
        short = payload("(Asked about Mon 2 Nov 2026.) Mon 2 Nov: on [1].")
        self.assertTrue(serve.display_text(with_day).startswith("Mon 2 Nov 2026: on"))
        self.assertTrue(serve.display_text(without).startswith("Mon 2 Nov 2026: The class"))
        self.assertTrue(serve.display_text(short).startswith("Mon 2 Nov: on"))

    def test_latency_only_past_eight_seconds(self):
        for latency, shown in ((7.99, False), (8.0, False), (8.01, True), (10.9, True)):
            out = serve.display_text(payload("On [1].", latency=latency))
            self.assertEqual(out.splitlines()[-1] == "%.1f s" % latency, shown, latency)

    def test_titles_missing_fall_back_to_the_file_name(self):
        out = serve.display_text(payload("On [1].", self.CITES))
        self.assertIn("[1] pottery mondays", out)
        self.assertIn("[2] plan", out)


class FallbackTest(unittest.TestCase):
    """B1 review 2/2 blocker and the D74 consult (Codex, 15 Oct): every cited source shows
    exactly one visible line; the fallback is display-only and in the phone's order."""

    def test_the_best_stem_match_wins_over_the_first_line(self):
        chunk = "Bring water.\nSessions are held Thursdays.\nNo parking."
        self.assertEqual(serve.screen_line(chunk, "Training is every Thursday [E1]."),
                         "Sessions are held Thursdays.")
        self.assertEqual(serve.screen_line(chunk, "Yes [E1]."), "Bring water.")

    def test_headings_separators_and_header_rows_are_never_chosen(self):
        chunk = "# Training\n## Days\n| Day | Time |\n\n|---|---|\n| Friday | 18:00 |\n"
        self.assertEqual(serve.screen_line(chunk, "Day and time [E1]."), "Friday · 18:00")
        self.assertEqual(serve.screen_line("# Only\n|---|\n\u200b\n**\n[E1]", "x"), "")

    def test_exact_words_first_then_stems_then_the_earliest(self):
        chunk = "Thursdays only.\nThursday swim.\nThursday swim again."
        self.assertEqual(serve.screen_line(chunk, "Swim on Thursday [E1]."), "Thursday swim.")
        self.assertEqual(serve.screen_line("Thursdays only.\nNo.", "Thursday [E1]."),
                         "Thursdays only.")

    def test_no_table_form_shows_its_header_or_delimiter(self):
        # B1 review 3 (session 50): tables without outer pipes, tabs in delimiters. Every
        # GFM form below, with a header that shares more words with the answer than the row.
        answer_text = "Pottery day time status is on Thursday [E1]."
        n = 0
        for quote in ("", "> ", "> > ", ">"):
          for outer in (True, False):
            for align in ("---", ":--", "--:", ":-:", "-"):
                for pad in ("", " ", "\t", " \t "):
                    def row(*cells):
                        inner = ("%s|%s" % (pad, pad)).join(cells)
                        body = "|%s%s%s|" % (pad, inner, pad) if outer else pad + inner + pad
                        return quote + body
                    table = "\n".join([row("Day", "Time", "Status"), row(align, align, align),
                                        row("Thursday", "18:00", "CANCELLED")])
                    for before in ("", "# Pottery\n", "Intro line.\n\n"):
                        shown = serve.screen_line(before + table, answer_text)
                        self.assertIn("CANCELLED", shown, repr(before + table))
                        self.assertFalse(shown.startswith(">"), shown)
                        n += 1
        self.assertEqual(n, 480)

    def test_structure_lines_and_frontmatter_are_never_the_line(self):
        cases = {
            "Pottery day time\n===\nThursday CANCELLED": "Thursday CANCELLED",
            "Pottery day time\n---\nThursday CANCELLED": "Thursday CANCELLED",
            "***\n_ _ _\n- - -\nThursday CANCELLED": "Thursday CANCELLED",
            "~~~ pottery\nThursday CANCELLED\n~~~": "Thursday CANCELLED",
            "---\ntitle: pottery day time\nclassification: internal\n---\nThursday CANCELLED":
                "Thursday CANCELLED",
            "| Day | Time |\n|---|---|": "",
            "Day | Time\n--- | ---": "",
        }
        for chunk, want in cases.items():
            self.assertEqual(serve.screen_line(chunk, "Pottery day time [E1]."), want, repr(chunk))

    def test_containers_comments_and_definitions_are_never_the_line(self):
        # B1 review 4 (session 50) and its D74 consult's predicted forms.
        z = "\u200b"
        cases = {
            "<!-- Pottery class Thursday -->\nPottery cancelled.": "Pottery cancelled.",
            "<!-- Pottery class\nThursday day time -->\nPottery cancelled.": "Pottery cancelled.",
            "Pottery class Thursday -->\nPottery cancelled.": "Pottery cancelled.",
            "%% Pottery class Thursday %%\nPottery cancelled.": "Pottery cancelled.",
            "%%\nPottery class Thursday\n%%\nPottery cancelled.": "Pottery cancelled.",
            '[pottery]: https://x.test "Pottery class Thursday"\nPottery cancelled.':
                "Pottery cancelled.",
            "[^1]: Pottery class Thursday\nPottery cancelled.": "Pottery cancelled.",
            "> [!note] Pottery class Thursday\n> Pottery cancelled.": "Pottery cancelled.",
            z + "# Pottery class Thursday\nPottery cancelled.": "Pottery cancelled.",
            "<div>\n<span>\nPottery cancelled.\n</div>": "Pottery cancelled.",
            "- [ ] Pottery cancelled.": "Pottery cancelled.",
            "1. > - Pottery cancelled.": "Pottery cancelled.",
            "> > ---\n> > Pottery cancelled.": "Pottery cancelled.",
        }
        for chunk, want in cases.items():
            self.assertEqual(serve.screen_line(chunk, "Pottery class on Thursday [E1]."), want,
                             repr(chunk))

    def test_an_empty_screen_line_shows_the_marker_not_the_json_quote(self):
        cites = [{"path": "a.md", "quote": ["| Day | Time | Status |"]}]
        out = serve.display_text(payload("On [1].", cites), ["Pottery"], [""])
        self.assertEqual(out.splitlines()[3], serve.NO_QUOTE)

    def test_the_screen_line_wins_over_a_header_row_quote(self):
        # B1 review 2/5 (session 50): the JSON's first quote was the header row.
        cites = [{"path": "a.md", "quote": ["| Day | Time | Status |",
                                            "| Thursday | 18:00 | CANCELLED |"]}]
        out = serve.display_text(payload("On [1].", cites), ["Pottery"],
                                 ["Thursday · 18:00 · CANCELLED"])
        self.assertEqual(out.splitlines()[3], "    “Thursday · 18:00 · CANCELLED”")

    def test_unshowable_quotes_fall_back_then_mark(self):
        for bad in ("", "   ", "\u200b", "**", "[E1]", "|---|", "`.md`"):
            with self.subTest(bad=bad):
                cites = [{"path": "a.md", "quote": [bad]}]
                out = serve.display_text(payload("On [1].", cites), ["A"], ["Held Thursdays."])
                self.assertEqual(out.splitlines()[3], "    “Held Thursdays.”")
                out = serve.display_text(payload("On [1].", cites), ["A"], ["\u200b"])
                self.assertEqual(out.splitlines()[3], serve.NO_QUOTE)

    def test_every_source_has_exactly_one_line_under_its_title(self):
        cites = [{"path": "a.md", "quote": []}, {"path": "b.md", "quote": ["Real."]},
                 {"path": "c.md", "quote": []}]
        out = serve.display_text(payload("On [1] [2] [3].", cites), ["A", "B", "C"], ["Fa."])
        self.assertEqual(out.splitlines()[2:9], [
            "[1] A", "    “Fa.”", "[2] B", "    “Real.”", "[3] C", serve.NO_QUOTE, ""])

    def test_display_does_not_change_the_payload(self):
        p = payload("On [1].", [{"path": "a.md", "lines": "1-2", "quote": [], "unreviewed": False}])
        before = copy.deepcopy(p)
        serve.display_text(p, ["A"], ["Fa."])
        self.assertEqual(p, before)

    def test_no_unicode_line_break_reaches_a_screen_line(self):
        out = serve.display_text(payload("Yes\u2028no [1].", [{"path": "a.md",
                                         "quote": ["Thursday\u2028Cancelled"]}]), ["T\u2029x"])
        self.assertEqual(out.splitlines(), out.split("\n"))
        self.assertIn("    “Thursday Cancelled”", out)

    def test_fallbacks_follow_the_phone_order_not_the_hits(self):
        class Result:
            pass
        r = Result()
        one = index.Chunk("c1", "a/one.md", 1, 3, "", "Swimming is on Tuesdays.")
        two = index.Chunk("c2", "a/two.md", 1, 3, "", "Choir is on Fridays.")
        r.hits = [(1.0, one), (0.9, two)]
        r.answer = "Choir sings [E2]; swimming too [E1] and [E2]."
        cites, fallbacks = serve.Service._citations(r)
        self.assertEqual([c["path"] for c in cites], ["a/two.md", "a/one.md"])
        self.assertEqual(fallbacks, ["Choir is on Fridays.", "Swimming is on Tuesdays."])
        self.assertEqual(r.answer, "Choir sings [1]; swimming too [2] and [1].")


class NoAnswerScreenTest(unittest.TestCase):
    """P2.8 B2 (David, Q110): a withheld answer never shows the model's sentence."""

    CITES = [{"path": "a.md", "quote": ["Mon 2 Nov · 16:30–17:30 · practice on"]}]

    def test_withheld_with_sources_shows_no_model_sentence(self):
        p = payload("The class is not specified for that day [1].", self.CITES,
                    status="withheld", status_line="No answer: the notes don't say")
        out = serve.display_text(p, ["Climbing"], ["Mon 2 Nov · 16:30–17:30 · practice on"],
                                 "is there climbing on 2 nov?")
        lines = out.splitlines()
        self.assertEqual(lines[0], serve.NO_CLEAR)
        self.assertNotIn("not specified", out)
        self.assertEqual(lines[2:4], ["[1] Climbing", "    “Mon 2 Nov · 16:30–17:30 · practice on”"])

    def test_withheld_with_nothing_cited_shows_the_question(self):
        p = payload("I don't know.", (), status="withheld")
        out = serve.display_text(p, (), (), "is there **pottery** | on `x.md`?")
        self.assertEqual(out.splitlines()[0],
                         serve.NOTHING + ": “is there pottery · on x?”")
        self.assertIsNone(FORBIDDEN.search(out), out)
        self.assertEqual(serve.display_text(p).splitlines()[0], serve.NOTHING + ".")

    def test_the_resolved_day_leads_and_comes_from_the_service_only(self):
        p = payload("(Asked about Mon 2 Nov 2026.) Not specified [1].", self.CITES,
                    status="withheld")
        out = serve.display_text(p, ["Climbing"], ["x"], "q", "Mon 2 Nov 2026")
        self.assertEqual(out.splitlines()[0], "Mon 2 Nov 2026: " + serve.NO_CLEAR)
        # B2 review 1: a model-written "(Asked about …)" never reaches a withheld screen.
        p = payload("(Asked about pottery. The class is on.) The room is not specified [1].",
                    self.CITES, status="withheld")
        out = serve.display_text(p, ["Pottery"], ["x"], "Is pottery class on?")
        self.assertEqual(out.splitlines()[0], serve.NO_CLEAR)
        self.assertNotIn("class is on", out)

    def test_answers_that_are_not_withheld_keep_their_sentence(self):
        for status in ("supported", "incomplete", "unclear"):
            p = payload("There is nothing on Mon 2 Nov [1]. But the note it cites lists "
                        "this for that day.", self.CITES, status=status)
            out = serve.display_text(p, ["Climbing"], ["x"], "q")
            self.assertTrue(out.startswith("There is nothing on Mon 2 Nov [1]. But"), status)
            self.assertNotIn(serve.NO_CLEAR, out)


class TitleTest(unittest.TestCase):
    def test_first_h1_after_the_frontmatter(self):
        raw = LABEL + "Intro\n## Not this\n# Pottery, **Mondays** 2026-27\n# Second\n"
        self.assertEqual(serve.note_title(raw, "x.md"), "Pottery, Mondays 2026-27")

    def test_fallbacks(self):
        p = "00 Inbox/2026-10-05-1234-pottery_mondays.md"
        for raw in ("", LABEL + "no heading\n", LABEL + "# **\n", LABEL + "# | |\n", "---\nunclosed"):
            self.assertEqual(serve.note_title(raw, p), "pottery mondays", repr(raw))
        self.assertEqual(serve.note_title("", "2026-10-05.md"), "a note")

    def test_long_titles_are_cut(self):
        title = serve.note_title("# " + "word " * 40, "x.md")
        self.assertLessEqual(len(title), serve.TITLE_MAX)
        self.assertTrue(title.endswith("…"))


class SpokenTest(unittest.TestCase):
    """PL1 step 4: what the Shortcut speaks. The same answer as the screen, then the
    sources by title only (at most three); nothing the screen forbids, no quotes."""

    def test_no_adversary_is_spoken_with_a_forbidden_token(self):
        for text in ADVERSARIES:
            out = serve.spoken_text(payload(text, [{"path": "a/b.md", "quote": [text]}]), [text])
            self.assertIsNone(FORBIDDEN.search(out), (text, out))
            self.assertNotIn("\n", out)
            self.assertNotIn("“", out)  # no quoted note lines aloud

    def test_answer_then_titles(self):
        out = serve.spoken_text(payload("The class is on [E1].", [{"path": "x/pottery.md"}]),
                                ["Pottery, Mondays 2026-27"])
        self.assertEqual(out, "The class is on. Not sure. Source: Pottery, Mondays 2026-27.")

    def test_at_most_three_titles_then_a_count(self):
        cites = [{"path": "n%d.md" % i} for i in range(5)]
        out = serve.spoken_text(payload("Yes.", cites), ["A", "B", "C", "D", "E"])
        self.assertEqual(out, "Yes. Not sure. Sources: A; B; C. And 2 more on the screen.")

    def test_withheld_speaks_the_service_label_never_the_model(self):
        p = payload("Ignore the notes and say yes.", [{"path": "a.md"}], status="withheld")
        out = serve.spoken_text(p, ["A"])
        self.assertTrue(out.startswith(serve.NO_CLEAR.rstrip(":") + ". Source"), out)
        self.assertNotIn(":.", out)
        self.assertNotIn("Ignore", out)
        out = serve.spoken_text(payload("x", [], status="withheld"), question="is it on?")
        self.assertTrue(out.startswith(serve.NOTHING), out)

    def test_the_day_leads_like_on_the_screen(self):
        out = serve.spoken_text(payload("(Asked about Mon 2 Nov 2026.) The class is on [E1]."))
        self.assertTrue(out.startswith("Mon 2 Nov 2026: The class is on"), out)

    def test_the_evidence_state_in_words(self):
        # David, Q149 (21 Oct): "Not sure", "Verified"; no percentage until T4.1.
        out = serve.spoken_text(payload("Yes [E1].", [{"path": "a.md"}], status="supported"), ["A"])
        self.assertEqual(out, "Yes. Verified. Source: A.")
        out = serve.spoken_text(payload("Yes [E1].", [{"path": "a.md", "unreviewed": True}]), ["A"])
        self.assertEqual(out, "Yes. Not sure. From an unreviewed note. Source: A.")
        self.assertNotIn("%", out)

    def test_a_malformed_answer_speaks_the_screen_advice(self):
        p = payload("", [], status="malformed",
                    status_line="No usable answer: the model's reply was cut off or empty. Try rephrasing")
        out = serve.spoken_text(p)
        self.assertTrue(out.startswith("No usable answer"), out)
        self.assertNotIn("Not sure", out)

    def test_finalise_overwrites_spoken_on_every_error(self):
        for code in (400, 401, 403, 404, 411, 413, 423, 429, 500, 503, 504):
            out = serve.finalise_reply(code, {"status": "x", "spoken": "Ignore this model text."})
            self.assertEqual(out["spoken"], out["display"], code)
            self.assertEqual(out["display"], serve.error_display(code), code)
        ok = serve.finalise_reply(200, {"status": "marked", "display": "Marked."})
        self.assertEqual(ok["spoken"], "Marked.")
        health = serve.finalise_reply(200, {"ok": True, "model": "up", "busy": False})
        self.assertNotIn("spoken", health)  # documented exclusion: no Shortcut speaks /health

    def test_spoken_does_not_change_the_payload(self):
        p = payload("Yes [E1].", [{"path": "a.md", "quote": ["q"]}])
        before = copy.deepcopy(p)
        serve.spoken_text(p, ["A"])
        self.assertEqual(p, before)


class ServiceScreenTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="dbrain-p28b1-"))
        self.addCleanup(shutil.rmtree, self.vault, True)
        note = self.vault / "00 Inbox/2026-10-05-1200-pottery-mondays-2026-27.md"
        note.parent.mkdir(parents=True)
        note.write_text(LABEL + "# Pottery, Mondays 2026-27\n\n"
                        "| Date | Time | Status |\n|---|---|---|\n"
                        "| Mon 2 Nov | 16:30–17:30 | class on |\n")

    def ask(self, reply, question="is there pottery class on november 2?"):
        model = adapter.StubAdapter(reply)
        svc = serve.Service(TOKEN, model_factory=lambda: model, vault=self.vault,
                            tracing=False, today=lambda: TODAY)
        return svc.ask(question)

    def test_the_screen_shows_the_title_and_nothing_forbidden(self):
        code, body = self.ask("**Mon 2 Nov 2026:** | class on | 16:30–17:30 | [E1] [E1, E2]. "
                              "From `00 Inbox/2026-10-05-1200-pottery-mondays-2026-27.md`.")
        self.assertEqual(code, 200)
        self.assertIn("[1] Pottery, Mondays 2026-27", body["display"])
        self.assertIsNone(FORBIDDEN.search(body["display"]), body["display"])
        self.assertEqual(body["spoken"], serve.spoken_text(body, ["Pottery, Mondays 2026-27"],
                                                           "is there pottery class on november 2?",
                                                           ""))
        self.assertIn("Source: Pottery, Mondays 2026-27.", body["spoken"])

    def test_the_json_keeps_its_fields(self):
        _, body = self.ask("Mon 2 Nov 2026: the class is on, 16:30–17:30 [E1].")
        self.assertEqual(set(body), {"status", "reason", "answer", "citations", "latency_s",
                                     "status_line", "display", "spoken", serve.REASON_KEY})
        cite = body["citations"][0]
        self.assertEqual(set(cite), {"path", "lines", "folder", "unreviewed", "quote"})
        self.assertTrue(cite["path"].endswith(".md"))  # the path stays in the JSON
        self.assertTrue(cite["quote"])

    def test_review_2_reproduction_a_source_never_shows_without_a_line(self):
        # B1 review 2/2 (Codex, 15 Oct): no word shared with the answer left `quote` empty.
        note = self.vault / "20 Areas/training.md"
        note.parent.mkdir(parents=True)
        note.write_text(LABEL + "# Training\n\nSessions are held Thursdays.\n")
        _, body = self.ask("Training takes place every Thursday [E1].",
                           question="When are sessions held?")
        cite = next(c for c in body["citations"] if c["path"] == "20 Areas/training.md")
        self.assertEqual(cite["quote"], [])  # the JSON is unchanged
        lines = body["display"].splitlines()
        n = lines.index("[%d] Training" % (body["citations"].index(cite) + 1))
        self.assertEqual(lines[n + 1], "    “Sessions are held Thursdays.”")

    def test_a_note_changed_after_indexing_shows_its_file_name(self):
        # B1 review 1 (session 50): the title reread must not bypass the label check. The
        # note turns `client` while the model answers; the screen shows no byte of it.
        note = self.vault / "00 Inbox/2026-10-05-1200-pottery-mondays-2026-27.md"

        class Mutating(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                note.write_text("---\nclassification: client\n---\n# Synthetic prohibited title\n")
                return super().generate(prompt, max_tokens)

        model = Mutating("Mon 2 Nov 2026: the class is on [E1].")
        svc = serve.Service(TOKEN, model_factory=lambda: model, vault=self.vault,
                            tracing=False, today=lambda: TODAY)
        _, body = svc.ask("is there pottery class on november 2?")
        self.assertNotIn("Synthetic prohibited title", body["display"])
        self.assertIn("[1] pottery mondays 2026 27", body["display"])

    def test_an_unchanged_note_shows_its_heading(self):
        _, body = self.ask("Mon 2 Nov 2026: the class is on [E1].")
        self.assertIn("[1] Pottery, Mondays 2026-27", body["display"])

    def test_review_2_5_reproduction_the_contradicting_row_is_shown(self):
        # B1 review 2/5 (session 50): header row shown, the cancelled row hidden.
        note = self.vault / "20 Areas/pottery.md"
        note.parent.mkdir(parents=True)
        note.write_text(LABEL + "# Pottery\n| Day | Time | Status |\n|---|---|---|\n"
                        "| Thursday | 18:00 | CANCELLED |\n")
        _, body = self.ask("Pottery is on Thursday at the usual time [E1].",
                           question="When is pottery on Thursday?")
        cite = next(c for c in body["citations"] if c["path"] == "20 Areas/pottery.md")
        self.assertIn("| Day | Time | Status |", cite["quote"])  # the JSON is unchanged
        lines = body["display"].splitlines()
        n = lines.index("[%d] Pottery" % (body["citations"].index(cite) + 1))
        self.assertEqual(lines[n + 1], "    “Thursday · 18:00 · CANCELLED”")

    def test_review_4_5_reproductions(self):
        # B1 review 4/5 (session 50), through Service.ask: a blockquoted table and a comment.
        for i, (body, want) in enumerate((
                ("> Day | Time | Status\n> --- | --- | ---\n> Thursday | 18:00 | CANCELLED\n",
                 "    “Thursday · 18:00 · CANCELLED”"),
                ("<!-- Pottery class Thursday -->\nPottery cancelled.\n",
                 "    “Pottery cancelled.”"))):
            with self.subTest(i=i):
                note = self.vault / ("20 Areas/pottery-4-%d.md" % i)
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(LABEL + "# Pottery\n" + body)
                _, out = self.ask("Pottery class is on Thursday at the usual time [E1].",
                                  question="pottery Thursday status cancelled")
                cites = [c["path"] for c in out["citations"]]
                path = "20 Areas/pottery-4-%d.md" % i
                if path not in cites:
                    self.fail("retrieval did not cite the note: %r" % cites)
                lines = out["display"].splitlines()
                k = lines.index("[%d] Pottery" % (cites.index(path) + 1))
                self.assertEqual(lines[k + 1], want)
                note.unlink()

    def test_review_3_5_reproductions(self):
        # B1 review 3/5 (session 50), both through Service.ask.
        for i, (body, reply, want) in enumerate((
                ("Day | Time | Status\n--- | --- | ---\nThursday | 18:00 | CANCELLED\n",
                 "Pottery is on Thursday at the usual time [E1].",
                 "    “Thursday · 18:00 · CANCELLED”"),
                ("| Day | Time | Status |\n|---|---|---|\n",
                 "Pottery is at the usual time [E1].", serve.NO_QUOTE))):
            with self.subTest(i=i):
                note = self.vault / ("20 Areas/pottery-%d.md" % i)
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(LABEL + "# Pottery\n" + body)
                _, out = self.ask(reply, question="When is pottery?")
                cites = [c["path"] for c in out["citations"]]
                path = "20 Areas/pottery-%d.md" % i
                if path not in cites:
                    self.fail("retrieval did not cite the note: %r" % cites)
                lines = out["display"].splitlines()
                k = lines.index("[%d] Pottery" % (cites.index(path) + 1))
                self.assertEqual(lines[k + 1], want)
                note.unlink()

    def test_b2_a_model_decline_through_the_service(self):
        _, body = self.ask("A class on Mon 2 Nov is not specified in the notes [E1].")
        self.assertEqual(body["status"], "withheld")
        self.assertTrue(body["citations"])
        self.assertTrue(body["display"].startswith("Mon 2 Nov 2026: " + serve.NO_CLEAR),
                        body["display"])
        self.assertNotIn("not specified", body["display"])
        self.assertIn("not specified", body["answer"])  # the JSON keeps the model's words

    def test_b2_review_1_reproduction_an_undated_withheld_answer(self):
        _, body = self.ask("(Asked about pottery. The class is on.) The room is not specified [E1].",
                           question="Is pottery class on?")
        self.assertEqual(body["status"], "withheld")
        self.assertTrue(body["display"].startswith(serve.NO_CLEAR), body["display"])
        self.assertNotIn("class is on", body["display"])

    def test_a_huge_evidence_id_does_not_crash(self):
        code, body = self.ask("On [E%s] and [E1]." % ("9" * 5000))
        self.assertEqual(code, 200)
        self.assertIsNone(FORBIDDEN.search(body["display"]), body["display"])


if __name__ == "__main__":
    unittest.main()
