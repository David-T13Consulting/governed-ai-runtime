"""P2.5 F3: the alias table and the question expansion (synthetic questions only)."""
import unittest

from dbrain import aliases

TABLE = {("trailblazers", "trailblazer"): ("hiking", "outing"),
         ("cantata",): ("choir", "rehearsal")}


class ExpandTest(unittest.TestCase):

    def test_an_alias_appends_the_note_wording(self):
        self.assertEqual(aliases.expand("Trailblazers on Saturday?", TABLE),
                         "Trailblazers on Saturday? (also: hiking, outing)")

    def test_case_and_accents_do_not_matter(self):
        for q in ("TRAILBLAZER today?", "trailblazérs today?", "trailblaze\u0301rs today?"):
            self.assertTrue(aliases.expand(q, TABLE).endswith("(also: hiking, outing)"), q)

    def test_no_alias_leaves_the_question_unchanged(self):
        for q in ("Is there choir on Sat 10 Oct?", "",
                  "trailblazersclub on Saturday?", "pre-cantata notes"):
            self.assertEqual(aliases.expand(q, TABLE), q)

    def test_words_already_asked_are_not_repeated(self):
        self.assertEqual(aliases.expand("Cantata or choir rehearsal?", TABLE),
                         "Cantata or choir rehearsal?")
        self.assertEqual(aliases.expand("Cantata at the choir?", TABLE),
                         "Cantata at the choir? (also: rehearsal)")

    def test_several_aliases_each_add_once_in_table_order(self):
        self.assertEqual(aliases.expand("Cantata, trailblazers, cantata?", TABLE),
                         "Cantata, trailblazers, cantata? (also: hiking, outing, choir, rehearsal)")

    def test_the_real_table_holds_the_seed_and_generic_words_only(self):
        self.assertEqual(aliases.expand("Cantatas samedi ?"), "Cantatas samedi ? (also: choir, rehearsal)")
        self.assertEqual(aliases.expand("cantata today?"), "cantata today? (also: choir, rehearsal)")
        for keys, terms in aliases.ALIASES.items():
            for word in keys + terms:
                self.assertRegex(word, r"^[a-zà-ÿ' -]+$")  # lower-case common words, no names

    def test_none_is_an_empty_question(self):
        self.assertIsNone(aliases.expand(None, TABLE))



class RescoreKeepsAliasesTest(unittest.TestCase):
    """F3 review 1, should-fix: a rescored report keeps the alias table it was run with."""

    def test_round_trip(self):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "p25", Path(__file__).resolve().parent.parent / "experiments" / "p25_regression.py")
        p25 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(p25)
        doc = p25.load_cases()
        old = {"today": doc["today"], "aliases": [[["cantata"], ["choir"]]], "cases": [
            dict(id=c["id"], status="incomplete", answer="x") for c in doc["cases"]]}
        self.assertEqual(p25.rescore(old, doc)["aliases"], [[["cantata"], ["choir"]]])


if __name__ == "__main__":
    unittest.main()
