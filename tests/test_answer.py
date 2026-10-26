import datetime
import unittest

from dbrain import adapter, answer, config, index, scope
from tests import fake_vault


class VerdictTest(unittest.TestCase):
    def setUp(self):
        self.vault, _, _ = fake_vault.build()
        self.profile = fake_vault.PROFILE

    def ask(self, question, reply=None, verifier=None):
        model = adapter.StubAdapter(reply=reply)
        result = answer.ask(question, profile=self.profile, model=model, vault=self.vault,
                            verifier=verifier)
        return result, model

    @staticmethod
    def confirming_verifier():
        """Stands in for the entailment check this module does not implement, so the
        plumbing around `supported` can still be tested."""
        return lambda text, chunks: True

    def test_no_evidence_withholds_and_never_calls_the_model(self):
        result, model = self.ask("What is the capital of France?")
        self.assertEqual(result.status, answer.WITHHELD)
        self.assertEqual(result.citations, [])
        self.assertEqual(model.calls, [], "the model was called with nothing to ground on")

    def test_grounded_answer_with_valid_citation_is_supported_given_a_verifier(self):
        result, _ = self.ask("What does tempo mean?", reply="Marathon pace or faster [E1]",
                             verifier=self.confirming_verifier())
        self.assertEqual(result.status, answer.SUPPORTED)
        self.assertTrue(result.citations)
        self.assertRegex(result.citations[0], r"\.md:\d+-\d+$")

    def test_answer_citing_nothing_is_incomplete_not_supported(self):
        result, _ = self.ask("What does tempo mean?", reply="Marathon pace or faster.")
        self.assertEqual(result.status, answer.INCOMPLETE)
        self.assertEqual(result.citations, [])

    def test_invented_citation_is_rejected_and_downgrades_the_verdict(self):
        result, _ = self.ask("What does tempo mean?", reply="Says so [E1] and also [E99]")
        self.assertEqual(result.status, answer.INCOMPLETE)
        self.assertIn("E99", result.invalid)

    def test_a_bare_citation_with_no_answer_text_is_not_supported(self):
        """An answer that is only a citation is not an answer."""
        result, _ = self.ask("What does tempo mean?", reply="[E1]")
        self.assertEqual(result.status, answer.INCOMPLETE)

    def test_citations_name_a_path_and_line_range(self):
        result, _ = self.ask("What does tempo mean?",
                             reply="Tempo means marathon pace or faster [E1]",
                             verifier=self.confirming_verifier())
        self.assertEqual(result.status, answer.SUPPORTED)
        path, lines = result.citations[0].rsplit(":", 1)
        self.assertTrue(path.endswith(".md"))
        start, end = lines.split("-")
        self.assertLessEqual(int(start), int(end))

    def test_evidence_given_to_the_model_comes_only_from_the_allowlist(self):
        result, model = self.ask("What does tempo mean?", reply="[E1]")
        prompt = model.calls[0]
        self.assertNotIn("Not in any allowlist", prompt)   # 90 System/secrets.md
        self.assertNotIn("Read-only unless asked", prompt)  # vault-root AGENTS.md


class ReasonCodeTest(unittest.TestCase):
    """P2.7 step C1b (D113): every verdict carries a fixed reason code, so a cited and
    grounded but unverified answer is told apart from a real defect. Status and readable
    reason stay exactly as before."""

    def setUp(self):
        self.vault, _, _ = fake_vault.build()
        self.profile = fake_vault.PROFILE

    def ask(self, reply=None, question="What does tempo mean?", verifier=None):
        return answer.ask(question, profile=self.profile, vault=self.vault,
                          model=adapter.StubAdapter(reply=reply), verifier=verifier)

    def test_each_path_gets_its_code_and_keeps_its_status(self):
        cases = [
            (dict(question="What is the capital of France?"), answer.WITHHELD, answer.NO_EVIDENCE),
            (dict(reply="The evidence does not say [E1]"), answer.WITHHELD, answer.DECLINED),
            (dict(reply="Says so [E1] and also [E99]"), answer.INCOMPLETE, answer.INVALID_CITATION),
            (dict(reply="Marathon pace or faster."), answer.INCOMPLETE, answer.NO_CITATION),
            (dict(reply="The moon is made of cheese [E1]"), answer.INCOMPLETE, answer.LOW_GROUNDING),
            (dict(reply="Tempo means marathon pace or faster [E1]"), answer.INCOMPLETE, answer.UNVERIFIED),
            (dict(reply="Tempo means marathon pace or faster [E1]", verifier=lambda t, c: False),
             answer.INCOMPLETE, answer.VERIFIER_REJECTED),
            (dict(reply="Tempo means marathon pace or faster [E1]", verifier=lambda t, c: True),
             answer.SUPPORTED, answer.VERIFIED),
        ]
        for kwargs, status, code in cases:
            with self.subTest(code=code):
                result = self.ask(**kwargs)
                self.assertEqual((result.status, result.reason_code), (status, code))
                self.assertIsInstance(result.reason_code, str)

    def test_the_codes_are_these_literal_strings(self):
        """The codes are a contract with the capture step (C2) and the audit counts: pin
        them as literals, not through the constants they are compared with."""
        self.assertEqual(
            (answer.NO_EVIDENCE, answer.DECLINED, answer.INVALID_CITATION, answer.NO_CITATION,
             answer.LOW_GROUNDING, answer.UNVERIFIED, answer.VERIFIER_REJECTED,
             answer.VERIFIED, answer.MODEL_UNAVAILABLE),
            ("no_evidence", "declined", "invalid_citation", "no_citation", "low_grounding",
             "unverified", "verifier_rejected", "verified", "model_unavailable"))
        self.assertEqual(self.ask(reply="The evidence does not say [E1]").reason_code, "declined")
        self.assertEqual(self.ask(reply="Tempo means marathon pace or faster [E1]").reason_code,
                         "unverified")

    def test_unverified_is_not_a_defect_and_the_real_defects_are(self):
        self.assertNotIn(answer.UNVERIFIED, answer.DEFECTS)
        self.assertEqual(answer.DEFECTS, {answer.INVALID_CITATION, answer.NO_CITATION,
                                          answer.LOW_GROUNDING, answer.VERIFIER_REJECTED,
                                          answer.NEGATION_VS_DATED})

    def test_the_readable_reason_is_unchanged(self):
        """The phone's status line is derived from the readable reason (serve.plain_status),
        so the reason text must stay what it was."""
        self.assertIn("entailment unverified",
                      self.ask(reply="Tempo means marathon pace or faster [E1]").reason)
        self.assertEqual(self.ask(reply="Marathon pace or faster.").reason,
                         "answered without citing any evidence")
        self.assertEqual(self.ask(reply="Says so [E1] and also [E99]").reason,
                         "cited an id that was never offered: E99")

    def test_a_model_outage_carries_its_code_on_the_partial_result(self):
        class Down(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                raise adapter.ModelUnavailable("down")
        with self.assertRaises(adapter.ModelUnavailable) as ctx:
            answer.ask("What does tempo mean?", profile=self.profile, vault=self.vault,
                       model=Down())
        self.assertEqual((ctx.exception.partial.status, ctx.exception.partial.reason_code),
                         (answer.FAILED, answer.MODEL_UNAVAILABLE))


class GroundingTest(unittest.TestCase):
    """A valid citation alone must not earn `supported`: the earlier version classified
    "The moon is made of cheese [E1]" as supported, which is the failure this guards."""

    def setUp(self):
        self.vault, _, _ = fake_vault.build()
        self.profile = fake_vault.PROFILE

    def ask(self, reply, question="What does tempo mean?"):
        return answer.ask(question, profile=self.profile,
                          model=adapter.StubAdapter(reply=reply), vault=self.vault)

    def test_an_offtopic_answer_citing_real_evidence_is_not_supported(self):
        for reply in (
            "The moon is made of cheese [E1]",
            "Bitcoin reached a new all-time high in Tokyo yesterday [E1]",
            "David should buy a Mac mini with 32 GB of unified memory [E1]",
        ):
            result = self.ask(reply)
            self.assertNotEqual(result.status, answer.SUPPORTED, reply)
            self.assertEqual(result.grounding, 0.0, reply)

    def test_a_model_declining_is_reported_as_withheld_not_as_an_answer(self):
        for reply in (
            "Insufficient evidence to answer [E1]",
            "The evidence does not say [E1]",
            "I don't know [E1]",
            "Not stated in the notes [E1]",
        ):
            result = self.ask(reply)
            self.assertEqual(result.status, answer.WITHHELD, reply)

    def test_a_faithful_paraphrase_is_grounded_but_not_claimed_as_supported(self):
        """Without a verifier the service must not claim support, even for a good answer."""
        for reply in (
            "Tempo means marathon pace or faster [E1]",
            "A tempo session is continuous work at marathon pace or faster [E1]",
        ):
            result = self.ask(reply)
            self.assertEqual(result.status, answer.INCOMPLETE, reply)
            self.assertIn("entailment unverified", result.reason)

    def test_supported_is_reachable_only_with_a_verifier(self):
        reply = "Tempo means marathon pace or faster [E1]"
        self.assertEqual(self.ask(reply).status, answer.INCOMPLETE)
        with_verifier = answer.ask(
            "What does tempo mean?", profile=self.profile, vault=self.vault,
            model=adapter.StubAdapter(reply=reply), verifier=lambda text, chunks: True,
        )
        self.assertEqual(with_verifier.status, answer.SUPPORTED)
        self.assertIn("entailment confirmed", with_verifier.reason)

    def test_a_verifier_that_rejects_downgrades_the_verdict(self):
        result = answer.ask(
            "What does tempo mean?", profile=self.profile, vault=self.vault,
            model=adapter.StubAdapter(reply="Tempo means marathon pace or faster [E1]"),
            verifier=lambda text, chunks: False,
        )
        self.assertEqual(result.status, answer.INCOMPLETE)
        self.assertIn("verifier rejected", result.reason)

    def test_a_contradiction_is_never_supported_even_with_high_overlap(self):
        """The case a lexical check cannot catch: same words, opposite meaning. It is kept
        out of `supported` by requiring a verifier, not by detecting the contradiction."""
        result = self.ask("Tempo means marathon pace or slower [E1]")
        self.assertNotEqual(result.status, answer.SUPPORTED)

    def test_the_verdict_carries_a_reason(self):
        self.assertIn("overlap", self.ask("Tempo means marathon pace or faster [E1]").reason)
        self.assertIn("threshold", self.ask("The moon is made of cheese [E1]").reason)

    def test_a_verifier_can_be_fooled_by_nothing_in_this_module(self):
        """The limitation is pinned where it belongs: in the docstring, and in the fact
        that `supported` cannot be produced here at all."""
        self.assertIn("unreachable unless a verifier", answer.__doc__ or "")


# P2.5 F2: synthetic notes only, unlike any real note.
ROW = "| Sat 10 Oct | Normal rehearsal | 10:00-12:00 |"
YEAR = "# Choir 2026\n"  # a chunk must carry the asked year (F2 D74 consult, 13 Oct)
HEAD = "| Date | Event | Time |\n|---|---|---|\n"  # the date sits under a Date column


def note(*rows, prose="", head=HEAD, year=YEAR):
    """A synthetic chunk: a year line, optional prose, a table header and `rows`."""
    return year + (prose + "\n" if prose else "") + head + "\n".join(rows)


CHOIR = note(ROW, "| Sat 17 Oct | No rehearsal (autumn break) | - |")
SAT10 = [datetime.date(2026, 10, 10)]
SAT17 = [datetime.date(2026, 10, 17)]
TUE13 = [datetime.date(2026, 10, 13)]
NO = "Nothing found for Sat 10 Oct 2026 [E1]."
NON = "Rien de prévu le samedi 10 octobre 2026 [E1]."


class DatedContradictionTest(unittest.TestCase):
    """P2.5 F2, narrow by design (D74 consult and David, 13 Oct): only a bare "nothing on
    <day>" answer against a clean cited table row for that day.

    Every silent case on a note is paired with a control (F2 review 1 of session 48): the
    same note without the defect must fire, so the case is silent for the reason it names."""

    def hit(self, text, cited=((1, CHOIR),), days=SAT10):
        return answer.dated_contradiction(text, list(cited), days)

    def silent(self, text, chunk, control, days=SAT10):
        self.assertIsNone(self.hit(text, [(1, chunk)], days), chunk)
        self.assertIsNotNone(self.hit(text, [(1, control)], days), control)

    def silent_row(self, text, row, days=SAT10, good=ROW):
        self.silent(text, note(row), note(good), days)

    def silent_prose(self, text, prose, row=ROW):
        self.silent(text, note(row, prose=prose), note(row))

    def test_a_bare_nothing_against_a_cited_row_fires_in_english_and_french(self):
        for text in ("Sat 10 Oct 2026: Nothing found for Sat 10 Oct 2026. [E1]",
                     "Nothing found for Sat 10 Oct 2026 [E1].",
                     "Nothing is scheduled on Saturday 10 October [E1]",
                     "No, there is nothing planned for 10 Oct [E1].",
                     "There are no events on 2026-10-10 [E1].",
                     "Nothing listed for October 10, 2026 in the notes [E1].",
                     "Rien de prévu le samedi 10 octobre [E1].",
                     "Il n’y a rien de prévu pour le 10 octobre 2026 [E1].",
                     "Aucun événement prévu le sam. 10 oct. [E1]"):
            self.assertEqual(self.hit(text), (1, ROW), text)

    def test_any_other_answer_is_left_alone(self):
        """Longer, activity-specific, partial or positive answers: misses by design."""
        for text in ("There is no cantata practice on Sat 10 Oct [E1].",
                     "No rehearsal [E1].", "Pas de répétition le 10 octobre [E1].",
                     "Nothing found [E1].", "Nothing found for that date [E1].",
                     "Nothing found for Sat 10 Oct 2026 [E1]. Rehearsal at 10:00.",
                     "Nothing found for Sun 10 Oct 2026 [E1].",
                     "Nothing found for Sat 10 Oct 2025 [E1].",
                     "Nothing found for Sat 3 Oct 2026 [E1].",
                     "Sat 10 Oct 2026: Normal rehearsal, 10:00-12:00 [E1].",
                     "The note does not use the word \"cantata\" [E1]."):
            self.assertIsNone(self.hit(text), text)
        self.assertIsNotNone(self.hit(NO))

    def test_only_cited_chunks_and_one_asked_day(self):
        self.assertIsNone(self.hit("Nothing found for Sat 10 Oct [E1].", cited=[]))  # F0 case d
        self.assertIsNone(self.hit("Nothing found for Tue 13 Oct [E1].", days=TUE13))
        self.assertIsNone(self.hit("Nothing found for Sat 10 Oct [E1].",
                                   days=SAT10 + [datetime.date(2026, 10, 11)]))
        self.assertIsNone(self.hit("Nothing found for Sat 10 Oct [E1].", days=[]))

    def test_a_note_row_that_negates_itself_never_fires_f0_case_c(self):
        self.assertIsNone(self.hit("Nothing found for Sat 17 Oct [E1].", days=SAT17))
        # ...while that row does not block the clean row of another day in the same table.
        self.assertEqual(self.hit("Nothing found for Sat 10 Oct [E1]."), (1, ROW))

    def test_rows_that_are_not_clean_never_fire(self):
        for row in ("| 10 Oct | Normal rehearsal | 10:00 |",          # no weekday, no year
                    "| Fri 10 Oct | Normal rehearsal | 10:00 |",      # another weekday
                    "| Sat 10 Oct 2025 | Normal rehearsal | 10:00 |",  # another year
                    "| Sat 10 Oct | 10:00-12:00 |",                    # no event word
                    "| Sat 10 Oct | Rehearsal moved to Sun 11 Oct | 10:00 |",
                    "| Sat 10 Oct | Rehearsal (to be confirmed) | 10:00 |",
                    "| Sat 10 Oct | Rehearsal? | 10:00 |",
                    "| Sat 10 Oct | Répétition reportée | 10h00 |",
                    "Sat 10 Oct: normal rehearsal at 10:00",            # not a table row
                    "Last updated: Sat 10 Oct 2026"):
            self.silent_row(NO, row)
        self.assertEqual(self.hit(NO, cited=[(1, HEAD + "| 2026-10-10 | Rehearsal |")]),
                         (1, "| 2026-10-10 | Rehearsal |"))
        self.assertEqual(self.hit(NO, cited=[(1, note("| samedi 10 octobre | Répétition | 10h |"))]),
                         (1, "| samedi 10 octobre | Répétition | 10h |"))

    def test_a_row_must_sit_under_a_date_column(self):
        """F2 review 1 of session 48: a logging date column is not the event's date."""
        self.silent(NO, note("| Sat 10 Oct | Rehearsal | 10:00 |",
                             head="| Logged | Event | Time |\n|---|---|---|\n"),
                    note("| Sat 10 Oct | Rehearsal | 10:00 |"))
        self.silent(NO, YEAR + "| Sat 10 Oct | Rehearsal | 10:00 |",  # no header at all
                    note("| Sat 10 Oct | Rehearsal | 10:00 |"))
        for head in ("| Day | Event |", "| Jour | Événement |", "| When | What |"):
            self.assertIsNotNone(self.hit(NO, [(1, note("| Sat 10 Oct | Rehearsal |",
                                                         head=head + "\n|---|---|\n"))]), head)

    def test_doubtful_prose_or_another_year_in_the_chunk_silences_it(self):
        for prose in ("All October rehearsals are cancelled.",
                      "Rehearsals moved for the autumn.",
                      "Les répétitions sont annulées.",
                      "Programme October '20 (copied in 2026)",  # review 1 of session 48
                      "Season 20-21",
                      "# Programme 2020"):
            self.silent_prose(NO, prose)
        self.silent(NO, "# Programme 2020\n" + HEAD + ROW, note(ROW))

    def test_review_examples_of_12_oct_verbatim(self):
        """F2 reviews 1 and 2 (Codex, 12 Oct): every example now stays silent; the three
        that review 1 wanted caught are not bare denials, so they are misses by design."""
        cases = [
            ("Normal rehearsal, not cantata practice, on Sat 10 Oct at 10:00 [E1].", ROW),
            ("Répétition normale le 10 octobre à 10h00, n’oubliez pas la séance [E1].", ROW),
            ("No rehearsal [E1].", "| Sat 10 Oct | There isn't any rehearsal | - |"),
            ("No rehearsal [E1].", "| Sat 10 Oct | Rehearsal won't take place | - |"),
            ("No rehearsal [E1].", "| Sat 10 Oct | Il n'y a plus de répétition | - |"),
            ("Nothing found for Sat 10 Oct 2026 [E1].", "| Fri 10 Oct 2025 | Normal rehearsal | 10:00 |"),
            ("Pas de cantate. Répétition normale de 10h à 12h [E1].", ROW),
            ("Nothing found for Sat 10 Oct [E1].", "Last updated: Sat 10 Oct 2026"),
            ("There is no need to book for the normal rehearsal at 10:00 [E1].", ROW),
            ("Il n’y a pas besoin de réserver pour la répétition à 10h00 [E1].", ROW),
            ("There is no cantata practice;normal rehearsal at 10:00 [E1].", ROW),
            ("There is no rehearsal on Sat 10 Oct [E1].",
             "| Sat 10 Oct | Rehearsal cannot take place | 10:00 |"),
            ("Nothing found for Sat 10 Oct [E1]. Rehearsal is at 10:00 on Sat 3 Oct [E1].", ROW),
            ("The note does not use the word cantata, and nothing is scheduled for Sat 10 Oct [E1].", ROW),
            ("There isn’t any rehearsal that day [E1].", ROW)]
        for text, line in cases:
            self.assertIsNone(self.hit(text, cited=[(1, note(line))]), (text, line))

    def test_d74_consult_cases_of_13_oct_verbatim(self):
        """The D74 consult (Codex, 13 Oct): its R row, and its ten cases."""
        r = "| Sat 10 Oct 2026 | Normal rehearsal | 10:00–12:00 |"
        fr = "| sam. 10 octobre 2026 | Répétition normale | 10h00–12h00 |"
        self.assertEqual(self.hit("Nothing scheduled for Sat 10 Oct 2026.", [(1, HEAD + r)]), (1, r))
        self.assertEqual(self.hit("Rien de prévu le 10 octobre 2026.", [(1, HEAD + fr)]), (1, fr))
        keep = [("No cantata practice; normal rehearsal takes place that day.", HEAD + r),
                ("Pas de cantate ; répétition normale ce jour-là.", HEAD + fr),
                ("There is no need to book; rehearsal starts at 10 am.", HEAD + r),
                ("Pas de répétition.", HEAD + "| sam. 10 octobre 2026 | Répétition supprimée | 10h00 |"),
                ("No rehearsal.", "All October rehearsals are cancelled.\n" + HEAD + r),
                ("Nothing scheduled that day.", HEAD + "| Sat 10 Oct 2026 | Rehearsal booking deadline | 10:00 |"),
                ("No cantata practice; normal rehearsal at 10:00.",
                 HEAD + r + "\n| Sat 10 Oct 2026 | Committee meeting | 15:00 |")]
        for text, chunk in keep:
            self.assertIsNone(self.hit(text, [(1, chunk)]), (text, chunk))
        no = "Nothing scheduled for Sat 10 Oct 2026."
        self.silent(no, "Programme 2020\n" + HEAD + "| Sat 10 Oct | Normal rehearsal | 10:00–12:00 |",
                    HEAD + r)
        self.silent(no, HEAD + "| Sat 10 Oct 2026 | Rehearsal booking deadline | 10:00 |", HEAD + r)

    def test_review_1_examples_of_13_oct_verbatim(self):
        """F2 review 1 (Codex, 13 Oct): cancellation nouns and numeric dates in other cells."""
        for text in (NO, NON):
            for row in ("| Sat 10 Oct | Rehearsal cancellation | - |",
                        "| Sat 10 Oct | Répétition : annulation | - |",
                        "| Sat 10 Oct | Répétition : néant | - |",
                        "| Sat 10 Oct | Rehearsal | 10.10 |",
                        "| Sat 10 Oct | Rehearsal, room 12 | 10:00 |",
                        "| Sat 10 Oct | School holiday | - |"):
                self.silent_row(text, row)
            for head, row in (("| Date | Event date (DD/MM) | Event | Time |",
                               "| Sat 10 Oct 2026 | 11/10 | Normal rehearsal | 10:00 |"),
                              ("| Date | Event date (DD/MM/YY) | Event | Time |",
                               "| Sat 10 Oct 2026 | 10/10/20 | Normal rehearsal | 10:00 |")):
                self.silent(text, note(row, head=head + "\n|---|---|---|---|\n"),
                            note("| Sat 10 Oct 2026 | Normal rehearsal | 10:00 |"))
        self.assertEqual(self.hit(NO, cited=[(1, note("| Sat 10 Oct | Rehearsal | 10h-12h |"))]),
                         (1, "| Sat 10 Oct | Rehearsal | 10h-12h |"))

    def test_review_2_examples_of_13_oct_verbatim(self):
        """F2 review 2 (Codex, 13 Oct): a "Non" status cell and a struck-through event."""
        self.silent(NON, note("| samedi 10 octobre 2026 | Répétition | Non |",
                              head="| Date | Événement | Maintenu |\n|---|---|---|\n"),
                    note("| samedi 10 octobre 2026 | Répétition | Oui |",
                         head="| Date | Événement | Maintenu |\n|---|---|---|\n"))
        for row in ("| Sat 10 Oct 2026 | ~~Normal rehearsal~~ | - |",
                    "| Sat 10 Oct 2026 | <del>Normal rehearsal</del> | - |"):
            self.silent_row(NO, row)

    def test_review_1_examples_of_13_oct_session_48_verbatim(self):
        """F2 review 1 of session 48 (Codex, 13 Oct): another day in words, and the
        should-fix markers (withdrawn, abolished, déprogrammée, a ❌ status)."""
        for text in (NO, NON):
            self.silent(text, note("| Sat 10 Oct | Rehearsal on Sunday the eleventh of October | 10:00 |",
                                   head="| Logged | Event | Time |\n|---|---|---|\n"),
                        note("| Sat 10 Oct | Rehearsal | 10:00 |"))
            for row in ("| Sat 10 Oct | Rehearsal on Sunday the eleventh of October | 10:00 |",
                        "| Sat 10 Oct | Rehearsal the next day | 10:00 |",
                        "| Sat 10 Oct | Répétition le lendemain | 10h |",
                        "| Sat 10 Oct | Répétition le onze | 10h |",
                        "| Sat 10 Oct | Rehearsal withdrawn | 10:00 |",
                        "| Sat 10 Oct | Rehearsal abolished | 10:00 |",
                        "| Sat 10 Oct | Répétition déprogrammée | 10h |",
                        "| Sat 10 Oct | Rehearsal | ❌ |",
                        "| Sat 10 Oct | Rehearsal | ✗ |"):
                self.silent_row(text, row)

    def test_the_code_is_a_defect(self):
        self.assertIn(answer.NEGATION_VS_DATED, answer.DEFECTS)

    def test_d74_consult_cases_of_13_oct_session_48_verbatim(self):
        """The D74 consult before F2's third review (Codex, 13 Oct): "mar" is both March and
        mardi; a year outside 1900-2099; a chunk split off under a heading without its year."""
        mar10 = [datetime.date(2026, 3, 10)]
        good = "| mar. 10 mars | Répétition |"
        for row in ("| mar. 10 | Répétition |", "| 10 mar | Répétition |"):
            self.silent_row("Rien de prévu le 10 mars 2026 [E1].", row, mar10, good)
        self.silent("Rien de prévu le 10 mars 2026 [E1].",
                    note("| mar. 10 | Répétition |", year="## Novembre 2026\n"), note(good), mar10)
        for year in ("# Programme 1885\n", "# Programme 2105\n", "## October\n"):
            self.silent(NO, note("| Sat 10 Oct | Rehearsal |", year=year),
                        note("| Sat 10 Oct | Rehearsal |"))

    def test_guard_matrix(self):
        """Wrong ISO day or year, either date order, repeated days, empty answers."""
        for row in ("| 2026-10-11 | Rehearsal |", "| 2025-10-10 | Rehearsal |",
                    "| 2026-11-10 | Rehearsal |"):
            self.silent_row(NO, row)
        for row in ("| Sat 10 Oct | Rehearsal |", "| Sat Oct 10 | Rehearsal |",
                    "| Saturday, October 10, 2026 | Rehearsal |", "| sam. 10 oct. | Répétition |"):
            self.assertEqual(self.hit(NO, [(1, note(row))]), (1, row), row)
        self.assertIsNone(self.hit(NO, days=SAT10 + SAT10))
        for text in ("", None, "[E1]"):
            self.assertIsNone(self.hit(text), text)

    def test_long_cells_stay_fast(self):
        """No regex in the check backtracks badly on a long cell or answer (session 48)."""
        import time
        started = time.monotonic()
        for row in ("| Sat 10 Oct | " + "-" * 50000 + "❌ |", "| Sat 10 Oct | " + "a" * 50000 + "teen! |"):
            self.assertIsNone(self.hit(NO, [(1, note(row))]))
        self.assertIsNone(self.hit("Nothing found for Sat 10 Oct 2026" + " " * 50000 + "x [E1]"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_review_2_examples_of_13_oct_session_48_verbatim(self):
        """F2 review 2 of session 48 (Codex, 13 Oct): a day named relative to the row, a year
        written in words, and the should-fix words (aborted, void, deferred, ajournée)."""
        for text in (NO, NON):
            for row in ("| Sat 10 Oct | Rehearsal the day after | 10:00 |",
                        "| Sat 10 Oct | Répétition le surlendemain | 10h |",
                        "| Sat 10 Oct | Rehearsal aborted | 10:00 |",
                        "| Sat 10 Oct | Rehearsal void | 10:00 |",
                        "| Sat 10 Oct | Rehearsal deferred | 10:00 |",
                        "| Sat 10 Oct | Répétition ajournée | 10h |"):
                self.silent_row(text, row)
            for year in ("# Programme October two thousand twenty (copied in 2026)\n",
                         "# Programme octobre deux mille vingt (copié en 2026)\n"):
                self.silent(text, note("| Sat 10 Oct | Rehearsal | 10:00 |", year=year),
                            note("| Sat 10 Oct | Rehearsal | 10:00 |"))

    def test_review_3_examples_of_13_oct_session_48_verbatim(self):
        """F2 review 3 of session 48 (Codex, 13 Oct): a two-digit year in another row, and
        the should-fix words (rescinded, résiliée, fortnight, quinzaine). Another row for
        another day is the normal schedule case and does not silence it."""
        good = "| Sat 10 Oct 2026 | Rehearsal | 10:00 |"
        self.silent(NO, note(good, "| - | Programme October '20 | - |"), note(good))
        self.assertEqual(self.hit(NO, [(1, note(good, "| 2026-10-11 | Concert | 20:00 |"))]),
                         (1, good))
        for text in (NO, NON):
            for row in ("| Sat 10 Oct | Rehearsal rescinded | 10:00 |",
                        "| Sat 10 Oct | Répétition résiliée | 10h |",
                        "| Sat 10 Oct | Rehearsal in a fortnight | 10:00 |",
                        "| Sat 10 Oct | Répétition dans une quinzaine | 10h |"):
                self.silent_row(text, row)

    def test_review_4_examples_of_13_oct_session_48_verbatim(self):
        """F2 review 4 of session 48 (Codex, 13 Oct): a two-digit year in a numeric date in
        another row, and the should-fix words (axed, décommandée)."""
        good = "| Sat 10 Oct 2026 | Rehearsal | 10:00 |"
        for other in ("| 11/10/20 | Concert | 20:00 |", "| 11.10.20 | Concert | 20:00 |",
                      "| 20-10-11 | Concert | 20:00 |"):
            self.silent(NO, note(good, other, year="# Programme 2026\n"),
                        note(good, year="# Programme 2026\n"))
        for text in (NO, NON):
            for row in ("| Sat 10 Oct 2026 | Rehearsal axed | 10:00 |",
                        "| Sat 10 Oct 2026 | Répétition décommandée | 10h |"):
                self.silent_row(text, row)

    def test_every_number_in_the_chunk_must_be_understood(self):
        """F2 D74 consult (15 Oct, session 49): numbers are allowed, not forbidden. Another
        year in a spelling no guard listed, or a numeric character other than ASCII 0-9, in
        a sibling row, the firing row or the heading, silences the check."""
        good = "| Sat 10 Oct 2026 | Rehearsal | 10:00 |"
        for text in (NO, NON):
            for other in ("| Sun 11 Oct 20 | Concert | 20:00 |",
                          "| 11 / 10 / 20 | Concert | 20:00 |",
                          "| 11⁄10⁄20 | Concert | 20:00 |",
                          "| 11/10/2026 | Concert | 20:00 |",   # numeric dates: not read
                          "| Sun 11 Oct 2020 | Concert | 20:00 |",
                          "| 2020-10-11 | Concert | 20:00 |",
                          "| Sun 45 Oct | Concert | 20:00 |",
                          "| Sun 11 Oct | Concert 2 | 20:00 |"):
                self.silent(text, note(good, other), note(good))
            for row in ("| Sat 10 Oct 2026 | Rehearsal ¹¹/¹⁰ | 10:00 |",
                        "| Sat 10 Oct 2026 | Rehearsal ₁₁/₁₀ | 10:00 |",
                        "| Sat 10 Oct 2026 | Rehearsal ²⁰²⁰ | 10:00 |",
                        "| Sat 10 Oct 2026 | Rehearsal ½ | 10:00 |",
                        "| Sat 10 Oct 2026 | Rehearsal ١١ | 10:00 |"):
                self.silent_row(text, row, good=good)
            self.silent(text, note(good, year="# Programme ²⁰²⁶ 2026\n"), note(good))

    def test_sibling_rows_for_other_days_of_the_year_still_let_it_fire(self):
        """The grammar reads written and ISO dates in the asked year, the year alone and
        times; other days in that year are expected (Q105)."""
        good = "| Sat 10 Oct 2026 | Rehearsal | 10:00–12:00 |"
        for other in ("| Sun 11 Oct 2026 | Concert | 20:00 |",
                      "| Sun 11 Oct | Concert | 20h |",
                      "| 2026-10-11 | Concert | 20h00 |",
                      "| dimanche 11 octobre | Concert | 20h |",
                      "| Oct 11, 2026 | Concert | 20:00 |"):
            self.assertIsNotNone(self.hit(NO, [(1, note(good, other))]), other)

    def test_scrubbed_is_a_cancellation(self):
        """F2 review 1 of session 49, should-fix (Q103)."""
        self.silent_row(NO, "| Sat 10 Oct 2026 | Rehearsal scrubbed | 10:00 |",
                        good="| Sat 10 Oct 2026 | Rehearsal | 10:00 |")
