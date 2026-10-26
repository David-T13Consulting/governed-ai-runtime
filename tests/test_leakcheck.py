"""P2.7 stage L: the leak check, tested with planted leaks only (no real question).

Every planted pair named in the P2.7 brief must be flagged, with the rule that catches it;
rewrites that share only function words must pass. The result never carries words.
"""
import unittest

from dbrain import leakcheck

# (original, synthetic, rule that must be among the hits) — all planted, all invented.
PLANTED = [
    # a capitalised name
    ("When does Emma have swimming?", "When does Emma have a lesson?", 1),
    # a lowercase name and its accent-folded form
    ("quel sac pour léo", "does leo need a bag", 4),
    # two-letter lowercase names
    ("what bag for li", "does li need a backpack", 4),
    ("quel sac pour bo", "does bo need a backpack", 4),
    # a short place
    ("wat meenemen naar spa", "what is needed in spa", 4),
    # a child's activity
    ("what must my child bring for climbing", "which equipment is needed for climbing", 4),
    # dates: resolved and unresolved, EN, FR, NL
    ("prepare my bag tomorrow", "wat meenemen morgen", 3),
    ("prepare my bag", "what to bring on 13 Oct", 3),
    ("menu ce jour-là", "quel menu demain", 3),
    ("menu le treize", "what is the menu on the thirteenth", 3),
    ("wat eten we", "wat eten we op dinsdag", 3),
    # a number
    ("is bus 47 late", "is the bus number 47 delayed", 2),
    # contact details in the synthetic, shared or not
    ("who do I call", "call jan@example.org about it", 5),
    ("who do I call", "call +32 470 12 34 56 about it", 5),
    ("which site", "see www.example.be for it", 5),
    # a near-copy
    ("what time is practice tonight", "what time is practice tonight please", 6),
]

# Rewrites sharing only function words. Sentence openers are capitalised in the original, so
# a clean rewrite must not reuse them (rule 1 counts every capitalised word).
CLEAN = [
    ("When does Emma have swimming?", "does a child have a sports lesson?"),
    ("quel sac pour léo", "which bag does a child need"),
    ("wat meenemen naar spa", "what should be packed for a trip"),
    ("Is there school on the 13th?", "could a class be held for a child?"),
]

# Rule 3, date text in the synthetic: resolved and unresolved, EN, FR and NL. The service's
# date reader never resolves Dutch dates; it finds them unresolved, which rule 3 counts.
DATE_MATRIX = [
    ("en", "resolved", "bring it tomorrow"),
    ("en", "unresolved", "bring it in summer"),
    ("en", "unresolved", "bring it on 31 Feb"),
    ("fr", "resolved", "apporter demain"),
    ("fr", "unresolved", "apporter cet été"),
    ("fr", "unresolved", "apporter le 31 février"),
    ("nl", "unresolved", "meenemen morgen"),
    ("nl", "unresolved", "meenemen op maandag"),
    ("nl", "word list", "meenemen in de zomer"),
]

# Regressions from the stage L review (8 Oct, round 1), each must be flagged by the rule.
REGRESSIONS = [
    ("Will needs a backpack for swimming", "will a child need equipment", 1),
    ("WILL needs a backpack", "will a child need equipment", 1),
    ("what is needed", "\uff2d\uff2f\uff2e", 3),                                  # fullwidth MON
    ("which site", "\uff48\uff54\uff54\uff50\uff53\uff1a\uff0f\uff0f\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45\uff0e\uff43\uff4f\uff4d", 5),
    ("who to write", "\uff41\uff4c\uff49\uff43\uff45\uff20\uff45\uff58\uff41\uff4d\uff50\uff4c\uff45\uff0e\uff44\uff45\uff56", 5),
    ("which site", "see ftp://example.dev for instructions", 5),
    ("which site", "see example.dev for instructions", 5),
    ("which site", "see file:/tmp/planted-note for instructions", 5),
    ("which site", "see data:text/plain,planted for instructions", 5),
    ("which site", "see example.\u6d4b\u8bd5 for instructions", 5),
    ("what is needed", "meegeven morgenavond", 3),
    ("what is needed", "meegeven vannacht", 3),
    ("what is needed", "meegeven vanochtend", 3),
    ("what is needed", "meegeven vanmiddag", 3),
    ("what is needed", "meegeven gisteravond", 3),
    ("what is needed", "meegeven morgenochtend", 3),
    ("what is needed", "bring it tonight", 3),
    ("what is needed", "apporter ce soir", 3),
    ("W\u030aill needs a backpack", "will a child need equipment", 1),
    ("which site", "see example.\u092d\u093e\u0930\u0924 for instructions", 5),
    ("which site", "see example.\u09ac\u09be\u0982\u09b2\u09be for instructions", 5),
    ("is my child just", "does a pupil seem just", 4),
    ("how very shy is my child", "is a pupil very reserved", 4),
    ("finish at midnight", "terminer \u00e0 minuit", 3),
    ("child starts at 14:30", "a pupil begins at 2pm", 3),
    ("child starts at 14:30", "a pupil begins at 2 pm", 3),
    ("which hour", "a pupil begins at 9 uur", 3),
    ("which contact", "reach 55  66  77  88", 5),
    ("which contact", "reach 55\u201366\u201377\u201388", 5),
    ("which contact", "reach 55\u2e3a66\u2e3a77\u2e3a88", 5),
    ("which contact", "reach 55\u2e4066\u2e4077\u2e4088", 5),
    ("which contact", "reach 55\u301c66\u301c77\u301c88", 5),
    ("which site", "see example\u3002com for instructions", 5),
    ("which site", "see example\uff61com for instructions", 5),
    ("who to write", "write to jan@example\u3002org", 5),
    ("child starts at 13:00", "a pupil begins at one o\u02bcclock", 3),
    ("which site", "see \u0938\u0902\u0938\u094d\u0925\u093e.\u092d\u093e\u0930\u0924 for instructions", 5),
    ("which site", "see \u09ac\u09be\u0982\u09b2\u09be.\u09ac\u09be\u0982\u09b2\u09be for instructions", 5),
    ("child starts at 13:00", "a pupil begins at one o\u2019clock", 3),
    ("child starts at 12:30", "een leerling begint om half een", 3),
    ("child starts at 06:00", "a pupil begins at dawn", 3),
    ("child starts at 06:00", "un \u00e9l\u00e8ve commence \u00e0 l\u2019aube", 3),
    ("which tools do I own", "what does a child own", 4),
    ("what is she doing", "what is a child doing", 4),
    ("Can you ask Will?", "will it rain", 1),
    # non-Latin names, apostrophes, hyphens and possessives
    ("когда придёт Маша", "когда придёт маша", 1),
    ("Is Jean-Luc coming", "is jean coming", 1),
    ("is Emma's bag ready", "is emma ready", 1),
    ("aujourd'hui avec Zoé", "avec zoe", 1),
]


class LeakCheckTest(unittest.TestCase):
    def test_every_planted_leak_is_flagged_by_its_rule(self):
        for original, synthetic, rule in PLANTED:
            with self.subTest(synthetic=synthetic):
                result = leakcheck.check(original, synthetic)
                self.assertTrue(result.flagged)
                self.assertIn(rule, result.rules_hit)

    def test_rewrites_sharing_only_function_words_pass(self):
        for original, synthetic in CLEAN:
            with self.subTest(synthetic=synthetic):
                result = leakcheck.check(original, synthetic)
                self.assertEqual(result, leakcheck.Result(False, ()), result)

    def test_the_result_holds_rule_numbers_only(self):
        for original, synthetic, _ in PLANTED:
            result = leakcheck.check(original, synthetic)
            self.assertEqual(set(result._fields), {"flagged", "rules_hit"})
            self.assertTrue(all(isinstance(r, int) and 1 <= r <= 6 for r in result.rules_hit))
            self.assertEqual(list(result.rules_hit), sorted(set(result.rules_hit)))

    def test_a_sentence_opener_counts_as_capitalised(self):
        self.assertIn(1, leakcheck.check("When is it?", "When was it?").rules_hit)
        self.assertIn(1, leakcheck.check("When is Marie there?", "is marie there").rules_hit)

    def test_accents_and_case_are_folded(self):
        self.assertIn(4, leakcheck.check("pour Zoé", "for zoe").rules_hit)
        self.assertIn(1, leakcheck.check("pour Zoé", "for ZOE").rules_hit)

    def test_dates_in_every_language_are_flagged_resolved_or_not(self):
        for lang, kind, synthetic in DATE_MATRIX:
            with self.subTest(lang=lang, kind=kind):
                self.assertIn(3, leakcheck.check("what to bring", synthetic).rules_hit)

    def test_review_regressions_are_flagged(self):
        for original, synthetic, rule in REGRESSIONS:
            with self.subTest(synthetic=synthetic):
                self.assertIn(rule, leakcheck.check(original, synthetic).rules_hit)

    def test_a_missing_or_unusable_original_flags(self):
        for original in ("", "?!", "\u201c", None, [], 42):
            with self.subTest(original=original):
                self.assertTrue(leakcheck.check(original, "for emma bring a backpack").flagged)
        self.assertTrue(leakcheck.check("anything", None).flagged)

    def test_a_bare_french_demonstrative_is_not_a_date(self):
        self.assertNotIn(3, leakcheck.check("ce sac appartient a leo",
                                            "ce paquet convient pour chaque enfant").rules_hit)

    def test_an_empty_rewrite_is_flagged(self):
        self.assertTrue(leakcheck.check("anything at all", "").flagged)
        self.assertTrue(leakcheck.check("anything at all", "?!").flagged)

    def test_no_activity_or_domain_word_is_on_the_function_list(self):
        for word in ("climbing", "school", "menu", "lunch", "bins", "swimming", "scouts", "spa"):
            self.assertNotIn(word, leakcheck.FUNCTION_WORDS)

    def test_a_date_reader_failure_flags(self):
        import dbrain.serve as serve
        saved = serve.resolve_dates
        serve.resolve_dates = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertIn(3, leakcheck.check("x", "a plain rewrite").rules_hit)
        finally:
            serve.resolve_dates = saved


if __name__ == "__main__":
    unittest.main()
