import unittest

from dbrain import index, scope
from tests import fake_vault


def assert_never_sent(case, prof, model, vault, msg, question="tempo ZZCLIENTZZ?"):
    """Q46: a refused note either stops the query (a recognisable `client` label, with no
    model call at all) or is skipped and traced; either way it never reaches the model."""
    from dbrain import answer
    try:
        result = answer.ask(question, profile=prof, model=model, vault=vault)
    except index.ProhibitedNote:
        case.assertEqual(model.calls, [], msg)
        return "stop"
    case.assertIn("20 Areas/Running/x.md", [p for p, _ in result.skipped], msg)
    case.assertFalse(any("Running/x.md" in str(c) for c in model.calls), msg)
    return "skip"


def assert_refused_by_build(case, prof, vault, msg):
    """A strict profile raises; a skipping one stops or records the note as skipped."""
    skipped = []
    try:
        index.build(prof, vault, skipped=skipped)
    except scope.ScopeError as exc:
        case.assertTrue(isinstance(exc, index.ProhibitedNote) or not prof.skip_unlabelled, msg)
        return
    case.assertTrue(prof.skip_unlabelled, msg)
    case.assertIn("20 Areas/Running/x.md", [p for p, _ in skipped], msg)

FORMS = [
    ("classification: client", "client"),
    ('classification: "client"', "client"),
    ("classification: 'client'", "client"),
    ("classification: client # legacy label", "client"),
    ("classification:   CLIENT  ", "client"),
    ("classification: internal", "internal"),
    ("classification: public", "public"),
]


class ClassificationParsingTest(unittest.TestCase):
    def test_quoted_commented_and_cased_forms_are_all_recognised(self):
        for line, expected in FORMS:
            raw = "---\n%s\n---\n# Note\n\ntext\n" % line
            self.assertEqual(index.classification_of(raw), expected, line)

    def test_a_missing_classification_is_not_silently_accepted(self):
        self.assertIsNone(index.classification_of("---\ntitle: x\n---\n# Note\n"))

    def test_conflicting_values_are_flagged_rather_than_picked(self):
        raw = "---\nclassification: internal\nclassification: client\n---\n"
        self.assertTrue(index.classification_of(raw).startswith("ambiguous:"))


class ClassificationEnforcementTest(unittest.TestCase):
    def build_with(self, line):
        vault, _, _ = fake_vault.build()
        (vault / "30 Resources/Running/suspect.md").write_text(
            "---\n%s\n---\n# Suspect\n\nCOUNTERPARTY_MARKER text.\n" % line
        )
        return vault

    def test_every_client_spelling_is_refused_at_index_time(self):
        for line, expected in FORMS:
            if expected != "client":
                continue
            vault = self.build_with(line)
            with self.assertRaises(scope.ScopeError, msg=line) as ctx:
                index.build(fake_vault.PROFILE, vault)
            self.assertIn("client", str(ctx.exception).lower())

    def test_a_note_with_no_classification_is_refused(self):
        vault = self.build_with("title: no label here")
        with self.assertRaises(scope.ScopeError) as ctx:
            index.build(fake_vault.PROFILE, vault)
        self.assertIn("no `classification`", str(ctx.exception))

    def test_an_unrecognised_classification_is_refused(self):
        vault = self.build_with("classification: top-secret")
        with self.assertRaises(scope.ScopeError) as ctx:
            index.build(fake_vault.PROFILE, vault)
        # Since round G an unrecognised label is a fail-closed stop, not just a refusal.
        self.assertIn("not plainly public or internal", str(ctx.exception))

    def test_a_permitted_note_still_indexes(self):
        vault = self.build_with("classification: internal")
        chunks, files = index.build(fake_vault.PROFILE, vault)
        self.assertIn("30 Resources/Running/suspect.md", files)


if __name__ == "__main__":
    unittest.main()


class ProhibitedNotesStopTest(unittest.TestCase):
    """Round E finding 5: skip_unlabelled may skip a missing label, never a breach."""

    def _vault(self, body):
        from tests import fake_vault
        vault, _, _ = fake_vault.build()
        (vault / "20 Areas/Running/x.md").write_text(body)
        return vault

    def test_a_client_note_stops_even_a_skipping_profile(self):
        from dbrain import config, index
        prof = config.Profile("skipper", roots=("20 Areas/Running",), skip_unlabelled=True)
        vault = self._vault("---\nclassification: client\n---\nCLIENT\n")
        with self.assertRaises(index.ProhibitedNote):
            index.build(prof, vault, skipped=[])

    def test_conflicting_labels_stop_too(self):
        from dbrain import config, index
        prof = config.Profile("skipper", roots=("20 Areas/Running",), skip_unlabelled=True)
        vault = self._vault("---\nclassification: internal\nclassification: client\n---\nX\n")
        with self.assertRaises(index.ProhibitedNote):
            index.build(prof, vault, skipped=[])

    def test_a_missing_label_is_still_skipped(self):
        from dbrain import config, index
        prof = config.Profile("skipper", roots=("20 Areas/Running",), skip_unlabelled=True)
        vault = self._vault("# no frontmatter\n")
        skipped = []
        index.build(prof, vault, skipped=skipped)
        self.assertEqual([p for p, _ in skipped], ["20 Areas/Running/x.md"])


class ProhibitedDuplicatesTest(unittest.TestCase):
    """Round F finding 2: a malformed sibling must not hide `client`."""

    def _build(self, body):
        from dbrain import config, index
        from tests import fake_vault
        vault, _, _ = fake_vault.build()
        (vault / "20 Areas/Running/x.md").write_text(body)
        prof = config.Profile("skipper", roots=("20 Areas/Running",), skip_unlabelled=True)
        return lambda: index.build(prof, vault, skipped=[])

    def test_client_with_a_malformed_sibling_stops_in_either_order(self):
        from dbrain import index
        for body in ("---\nclassification: client\nclassification: [internal]\n---\nX\n",
                     "---\nclassification: [internal]\nclassification: client\n---\nX\n",
                     "---\nclassification: [client]\n---\nX\n",
                     "---\nclassification: \"Client\" # odd\n---\nX\n",
                     "---\nclassification: client\nno closing fence\n"):
            with self.assertRaises(index.ProhibitedNote, msg=body):
                self._build(body)()

    def test_prose_mentioning_client_is_not_a_label(self):
        skipped_build = self._build("---\nclassification: internal\n---\nA client called.\n")
        skipped_build()  # no exception


class FailClosedLabelsTest(unittest.TestCase):
    """Round G: escaped keys, escaped values and YAML block/flow forms all stop, and
    prohibited content never reaches the model."""

    BODIES = {
        "escaped duplicate key": '---\nclassification: internal\n"classificatio\\u006e": client\n---\nZZCLIENTZZ\n',
        "escaped value": '---\nclassification: "cl\\u0069ent"\n---\nZZCLIENTZZ\n',
        "block scalar": "---\nclassification: |\n  client\n---\nZZCLIENTZZ\n",
        "folded scalar": "---\nclassification: >\n  client\n---\nZZCLIENTZZ\n",
        "block list": "---\nclassification:\n  - client\n---\nZZCLIENTZZ\n",
        "capitalised key": "---\nClassification: client\n---\nZZCLIENTZZ\n",
        "single-quoted key": "---\n'classification': client\n---\nZZCLIENTZZ\n",
        "anchor": "---\nclassification: &a client\n---\nZZCLIENTZZ\n",
    }

    def _vault(self, body):
        from tests import fake_vault
        vault, _, _ = fake_vault.build()
        (vault / "20 Areas/Running/x.md").write_text(body)
        return vault

    def test_every_form_stops_skipping_and_strict_profiles(self):
        from dbrain import config, index
        for name, body in self.BODIES.items():
            vault = self._vault(body)
            for prof in (config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True),
                         config.Profile("strict", roots=("20 Areas/Running",))):
                assert_refused_by_build(self, prof, vault, "%s / %s" % (name, prof.name))

    def test_prohibited_content_never_reaches_the_model(self):
        from dbrain import adapter, answer, config, index
        prof = config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True)
        for name, body in self.BODIES.items():
            assert_never_sent(self, prof, adapter.StubAdapter(), self._vault(body), name)

    def test_plain_labels_are_accepted(self):
        from dbrain import index
        for body in ("---\nclassification: internal\n---\nx\n",
                     "---\nclassification: 'public' # note\n---\nx\n",
                     '---\nclassification: "internal"\nsource: a client meeting\n---\nx\n'):
            self.assertIn(index._verified_label(body), ("public", "internal"), body)


class StructuralLabelsTest(unittest.TestCase):
    """Round H: the label is read from YAML structure (grammar + real parser), so complex
    keys, top-level flow maps, merge keys and continuations stop, while block-scalar
    content that merely mentions `classification: client` does not."""

    STOPS = {
        "complex key": "---\n? classification\n: client\nexample: |\n  classification: internal\n---\nZZCLIENTZZ\n",
        "top-level flow map": "---\n{classification: client}\n---\nZZCLIENTZZ\n",
        "merge key": "---\n<<: {classification: client}\n---\nZZCLIENTZZ\n",
        "continuation": "---\nclassification: internal\n  client\n---\nZZCLIENTZZ\n",
        "unterminated": "---\nclassification: client\nbody without fence\nZZCLIENTZZ\n",
        "tab indent": "---\nclassification: internal\n\tclassification: client\n---\nZZCLIENTZZ\n",
        # Outside the allowlisted language since the round K redesign (no vault note uses them):
        "block scalar mentions client": "---\nclassification: internal\nexample: |\n  classification: client\n---\nZZCLIENTZZ\n",
        "indented fence inside block": "---\nclassification: internal\nexample: |\n  ---\n  classification: client\n---\nZZCLIENTZZ\n",
        "nested list elsewhere": "---\nclassification: public\ntags:\n  - client\n---\nZZCLIENTZZ\n",
    }
    PASSES = {
    }

    def _vault(self, body):
        from tests import fake_vault
        vault, _, _ = fake_vault.build()
        (vault / "20 Areas/Running/x.md").write_text(body)
        return vault

    def test_unverifiable_structures_stop_and_never_reach_the_model(self):
        from dbrain import adapter, answer, config, index
        for name, body in self.STOPS.items():
            for prof in (config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True),
                         config.Profile("strict", roots=("20 Areas/Running",))):
                model = adapter.StubAdapter()
                if prof.skip_unlabelled:
                    assert_never_sent(self, prof, model, self._vault(body), "%s / %s" % (name, prof.name))
                else:
                    with self.assertRaises(scope.ScopeError, msg="%s / %s" % (name, prof.name)):
                        answer.ask("tempo ZZCLIENTZZ?", profile=prof, model=model, vault=self._vault(body))
                    self.assertEqual(model.calls, [], name)

    def test_legitimate_structures_pass(self):
        from dbrain import index
        for name, body in self.PASSES.items():
            self.assertIn(index._verified_label(body), ("public", "internal"), name)

    def test_fences_with_trailing_whitespace_are_fences(self):
        """Round I blocker: `--- ` read as "no frontmatter", so a client note was skipped
        instead of stopping."""
        from dbrain import index
        for body in ("--- \nclassification: client\n---\nZZCLIENTZZ\n",
                     "---\t\nclassification: client\n---\nZZCLIENTZZ\n",
                     "---\r\nclassification: client\r\n--- \r\nZZCLIENTZZ\n",
                     "---\nclassification: client\n...  \nZZCLIENTZZ\n"):
            with self.assertRaises(scope.ScopeError, msg=repr(body)):
                index._verified_label(body)
        self.assertEqual(index._verified_label("---  \nclassification: internal\n---   \nok\n"), "internal")

    def test_a_fence_followed_by_content_stops(self):
        from dbrain import index
        for body in ("--- classification: client\n---\nZZCLIENTZZ\n",
                     "---\nclassification: internal\n--- classification: client\nZZCLIENTZZ\n"):
            with self.assertRaises(scope.ScopeError, msg=repr(body)):
                index._verified_label(body)

    def test_fence_forms_stop_and_never_reach_the_model(self):
        from dbrain import adapter, answer, config, index
        prof = config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True)
        self.assertEqual("stop", assert_never_sent(
            self, prof, adapter.StubAdapter(),
            self._vault("--- \nclassification: client\n---\ntempo ZZCLIENTZZ\n"), "padded fence"))

    @unittest.skipIf(index._yaml is None, "needs PyYAML")
    def test_a_nested_complex_key_is_refused_not_a_crash(self):
        from dbrain import index
        with self.assertRaises(index.UnverifiableNote):
            index._verified_label("---\nclassification: internal\nfoo:\n  ? [a, b]\n  : c\n---\nok\n")

    def test_the_narrow_label_rule_q41(self):
        """Q41: only lowercase `classification` with `public`/`internal`, optionally quoted.
        Other spellings are valid YAML but stop, and the grammar alone enforces it."""
        from dbrain import index
        for body in ("---\n'classification': internal\n---\nx\n",
                     '---\n"classification": internal\n---\nx\n',
                     "---\nClassification: internal\n---\nx\n",
                     "---\nclassification: Internal\n---\nx\n",
                     "---\nclassification: PUBLIC\n---\nx\n"):
            with self.assertRaises(index.UnverifiableNote, msg=repr(body)):
                index._grammar_label(index._fence_split(body)[0])
            with self.assertRaises(index.UnverifiableNote, msg=repr(body)):
                index._verified_label(body)

    @unittest.skipIf(index._yaml is None, "needs PyYAML")
    def test_the_yaml_cross_check_catches_a_grammar_misreading(self):
        from dbrain import index
        with self.assertRaises(index.ProhibitedNote):
            index._verified_label("---\nClassification: client\n---\nx\n")


class RoundJTest(unittest.TestCase):
    """Round J: each form must stop with PyYAML *and* with the grammar alone, and the
    content must never reach the model."""

    STOPS = {
        "comment without space": "---\nclassification: internal#client\n---\nZZCLIENTZZ\n",
        "quoted value, comment without space": "---\nclassification: 'internal'#client\n---\nZZCLIENTZZ\n",
        "indented label, no parent": "---\n  classification: client\n---\nZZCLIENTZZ\n",
        "indented label under a one-line value": "---\nsource: chat\n  classification: client\n---\nZZCLIENTZZ\n",
        "unterminated flow map": "---\n{classification: client}\nZZCLIENTZZ\n",
        "unterminated escaped key": '---\n"classificatio\\u006e": client\nZZCLIENTZZ\n',
        "unterminated, no label at all": "---\ntitle: x\nZZCLIENTZZ\n",
        # Passed until the round K redesign; outside the allowlisted language now:
        "block scalar": "---\nclassification: internal\nexample: |\n  classification: client\n---\nZZCLIENTZZ\n",
        "nested map": "---\nclassification: internal\nmeta:\n  classification: other\n---\nZZCLIENTZZ\n",
        "plain continuation": "---\nclassification: public\nsummary: a long\n  value on two lines\n---\nZZCLIENTZZ\n",
    }
    PASSES = {
        "comment with space": "---\nclassification: internal # client meeting\n---\nok\n",
    }

    def _both_modes(self):
        from dbrain import index
        saved = index._yaml
        yield "with PyYAML" if saved is not None else "PyYAML absent"
        index._yaml = None
        try:
            yield "grammar alone"
        finally:
            index._yaml = saved

    def test_every_form_stops_in_both_modes(self):
        from dbrain import index
        for mode in self._both_modes():
            for name, body in self.STOPS.items():
                with self.assertRaises(scope.ScopeError, msg="%s / %s" % (name, mode)):
                    index._verified_label(body)

    def test_legitimate_forms_pass_in_both_modes(self):
        from dbrain import index
        for mode in self._both_modes():
            for name, body in self.PASSES.items():
                self.assertIn(index._verified_label(body), ("public", "internal"), "%s / %s" % (name, mode))

    def test_no_model_call_in_either_mode(self):
        from tests import fake_vault
        from dbrain import adapter, answer, config, index
        prof = config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True)
        for mode in self._both_modes():
            for name, body in self.STOPS.items():
                vault, _, _ = fake_vault.build()
                (vault / "20 Areas/Running/x.md").write_text(body)
                assert_never_sent(self, prof, adapter.StubAdapter(), vault, "%s / %s" % (name, mode))


class RoundKTest(RoundJTest):
    """Round K: values that run past their line, and every YAML line break."""

    STOPS = {
        "forged label in a double-quoted value": '---\nsource: "begin\nclassification: internal\nend: value"\n---\nZZCLIENTZZ\n',
        "same, plus invalid YAML elsewhere": '---\nsource: "begin\nclassification: internal\nend: value"\nother: invalid: value\n---\nZZCLIENTZZ\n',
        "forged label in a single-quoted value": "---\nsource: 'begin\nclassification: internal\nend'\n---\nZZCLIENTZZ\n",
        "forged label in a flow list": "---\ntags: [a,\nclassification: internal,\nb]\n---\nZZCLIENTZZ\n",
        "forged label in a nested quoted value": '---\nmeta:\n  x: "begin\nclassification: internal\n  end"\n---\nZZCLIENTZZ\n',
        "anchor before a quote": '---\nsource: &a "begin\nclassification: internal\nend"\n---\nZZCLIENTZZ\n',
        "tag before a quote": '---\nsource: !!str "begin\nclassification: internal\nend"\n---\nZZCLIENTZZ\n',
        "CR line endings": "---\rclassification: client\r---\rZZCLIENTZZ\r",
        "NEL line endings": "---\x85classification: client\x85---\x85ZZCLIENTZZ\x85",
        "LS line endings": "--- classification: client --- ZZCLIENTZZ ",
        "mixed CRLF and CR": "---\r\nclassification: client\r---\r\nZZCLIENTZZ\r\n",
        # Codex's round K reproducers, verbatim:
        "escaped key under a one-line value": '---\nclassification: internal\nsource: chat\n  "classificatio\\u006e": client\n---\nZZCLIENTZZ\n',
        "complex key under a one-line value": "---\nclassification: internal\nsource: chat\n  ? classification\n  : client\n---\nZZCLIENTZZ\n",
        "unterminated quoted value": '---\nclassification: internal\nsource: "open\n---\nZZCLIENTZZ\n',
        "bare CR hides the real label": '---\nsource: "start\nclassification: internal\nend: finish"\rclassification: client\n---\nZZCLIENTZZ\n',
        "NEL hides the real label": '---\nsource: "start\nclassification: internal\nend: finish"\x85classification: client\n---\nZZCLIENTZZ\n',
        # Passed until the redesign:
        "nested list of quoted values": '---\nclassification: internal\nmodels:\n  - "x"\n  - y\n---\nZZCLIENTZZ\n',
        "block scalar with an open quote": '---\nclassification: internal\nexample: |\n  he said "hello\n---\nZZCLIENTZZ\n',
    }
    PASSES = {
        "one-line quoted value": '---\nclassification: internal\nsource: "a: b # not a comment"\n---\nok\n',
        "one-line flow list with quotes": "---\nclassification: internal\ntags: [a, \"b]c\", 'd']\n---\nok\n",
    }


class AllowlistBoundaryTest(RoundJTest):
    """Round K redesign (Codex consultation, 5 Oct): the boundary of the accepted language,
    not only the latest reproducer. Every stop holds in both modes with no model call."""

    STOPS = {
        "identical duplicate label": "---\nclassification: internal\nclassification: internal\n---\nZZCLIENTZZ\n",
        "empty label": "---\nclassification:\n---\nZZCLIENTZZ\n",
        "label as a flow list": "---\nclassification: [internal]\n---\nZZCLIENTZZ\n",
        "label using the colon extension": "---\nclassification: internal: client\n---\nZZCLIENTZZ\n",
        "flow map value": "---\nclassification: internal\nmeta: {a: b}\n---\nZZCLIENTZZ\n",
        "flow list item read as a map": "---\nclassification: internal\ntags: [a: b]\n---\nZZCLIENTZZ\n",
        "flow list with trailing comma": "---\nclassification: internal\ntags: [a,]\n---\nZZCLIENTZZ\n",
        "content after a flow list": "---\nclassification: internal\ntags: [a] b\n---\nZZCLIENTZZ\n",
        "content after a quoted value": '---\nclassification: internal\nsource: "a" b\n---\nZZCLIENTZZ\n',
        "backslash in a value": '---\nclassification: internal\nsource: "a\\tb"\n---\nZZCLIENTZZ\n',
        "tab inside a value": "---\nclassification: internal\nsource: a\tb\n---\nZZCLIENTZZ\n",
        "indented comment": "---\nclassification: internal\n  # x\n---\nZZCLIENTZZ\n",
        "alias value": "---\nclassification: internal\nsource: *a\n---\nZZCLIENTZZ\n",
        "LS inside frontmatter": "---\nclassification: internal\nsource: a\u2028classification: client\n---\nZZCLIENTZZ\n",
        "opening fence with a CR": "---\rclassification: internal\n---\nZZCLIENTZZ\n",
        "closing fence with NEL": "---\nclassification: internal\n---\x85classification: client\n---\nZZCLIENTZZ\n",
    }
    PASSES = {
        "plain value with inner quotes": '---\nclassification: internal\nsource: Karpathy "LLM Wiki" gist\n---\nok\n',
        "flow list with comment": "---\nclassification: internal\nmodels: []            # e.g. [a, b\n---\nok\n",
        "flow list of plain items": "---\nclassification: internal\nmodels: [claude-opus-5 (lead), lens/pl, x\u2026]\n---\nok\n",
        "column-0 comment and blank line": "---\n# note\n\nclassification: 'public'\n---\nok\n",
        "BOM before the fence": "\ufeff---\nclassification: internal\n---\nok\n",
        "document-end closing fence": "---\nclassification: internal\n...\nok\n",
    }

    def test_no_frontmatter_and_no_label_are_verified_absence(self):
        from dbrain import index
        for mode in self._both_modes():
            for body in ("# just a note\n", "---\ntitle: x\n---\nok\n", "---\n---\nok\n"):
                self.assertIsNone(index._verified_label(body), "%r / %s" % (body, mode))


class Q46StopOrSkipTest(unittest.TestCase):
    """Q46 (David, 5 Oct): a block outside the allowlisted language is skipped and traced;
    a recognisable `client` label stops every query (rule 3), in either mode."""

    STOP = {
        "plain client": "---\nclassification: client\n---\nx\n",
        "hash without a space": "---\nclassification: internal#client\n---\nx\n",
        "capitalised key": "---\nClassification: client\n---\nx\n",
        "quoted key": "---\n'classification': client\n---\nx\n",
        "block list": "---\nclassification:\n  - client\n---\nx\n",
        "anchor": "---\nclassification: &a client\n---\nx\n",
        "indented, no parent": "---\n  classification: client\n---\nx\n",
        "padded fence": "--- \nclassification: client\n---\nx\n",
        "CR line endings": "---\rclassification: client\r---\rx\r",
        "NEL hides the real label": '---\nsource: "start\nclassification: internal\nend: finish"\x85classification: client\n---\nx\n',
        "unterminated": "---\nclassification: client\nbody\n",
        # Round M finding 2: no length limits on a recognisable label.
        "many spaces before client": "---\nmeta:\n  a: b\nclassification:" + " " * 65 + "client\n---\nx\n",
        "many spaces before the colon": "---\nmeta:\n  a: b\nclassification" + " " * 45 + ": client\n---\nx\n",
        "client on a deeper continuation": "---\nclassification:\n  -\n    client\n---\nx\n",
        "client after indented blank lines": "---\nclassification:\n  \n  client\n---\nx\n",
        "client after empty lines": "---\nclassification:\n\n\n  client\n---\nx\n",
        "escaped key naming client": '---\nclassification: internal\n"classificatio\\u006e": client\n---\nx\n',
    }
    SKIP = {
        "escaped value": '---\nclassification: "cl\\u0069ent"\n---\nx\n',
        "escaped key": '---\nclassification: internal\nsource: chat\n  "classificatio\\u006e": internal\n---\nx\n',
        "nested map": "---\nclassification: internal\nmeta:\n  a: b\n---\nx\n",
        "unknown label": "---\nclassification: secret\n---\nx\n",
        "capitalised value": "---\nclassification: Internal\n---\nx\n",
        "unterminated, no label": "---\ntitle: x\nbody\n",
        "duplicate keys": "---\nclassification: internal\nclassification: internal\n---\nx\n",
        # Q46 as written: plain LF text that both readers parse alike, so these are skipped now.
        "CRLF note": "---\r\nclassification: internal\r\n---\r\nx\r\n",
        "unquoted colon in a value": "---\nsource: chat (Project: X)\nclassification: internal\n---\nx\n",
        "value ending in a colon": "---\nclassification: public\nstatus: held:\n---\nx\n",
        # Round M finding 3: a comment, or the body after a `...` fence, is not a label.
        "client in a comment of a malformed block": "---\nclassification: internal # client policy\nmeta:\n  a: b\n---\nx\n",
        "client in the body after a ... fence": "---\nclassification: internal\nmeta:\n  a: b\n...\nclassification: client\n",
    }

    def _modes(self):
        saved = index._yaml
        yield "with PyYAML" if saved is not None else "PyYAML absent"
        index._yaml = None
        try:
            yield "grammar alone"
        finally:
            index._yaml = saved

    def test_recognisable_client_labels_stop(self):
        for mode in self._modes():
            for name, body in self.STOP.items():
                with self.assertRaises(index.ProhibitedNote, msg="%s / %s" % (name, mode)):
                    index._check_label(body, "x.md")

    def test_other_unverifiable_blocks_are_skipped(self):
        for mode in self._modes():
            for name, body in self.SKIP.items():
                with self.assertRaises(index.UnverifiableNote, msg="%s / %s" % (name, mode)):
                    index._check_label(body, "x.md")

    def test_a_skipped_note_is_traced_and_never_sent(self):
        from dbrain import adapter, config
        vault, _, _ = fake_vault.build()
        (vault / "20 Areas/Running/x.md").write_text(
            "---\nclassification: internal\nmeta:\n  a: b\n---\ntempo ZZSKIPZZ\n")
        prof = config.Profile("skip", roots=("20 Areas/Running",), skip_unlabelled=True)
        self.assertEqual("skip", assert_never_sent(self, prof, adapter.StubAdapter(), vault,
                                                   "nested map", question="tempo ZZSKIPZZ?"))
