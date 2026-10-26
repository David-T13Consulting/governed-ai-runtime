"""MVP-phone /ask service: the boundary, not the model (brief MVP-phone, Done when)."""
import datetime
import faulthandler
import http.client
import json
import os
import threading
import time
import unittest
from pathlib import Path

from dbrain import adapter, answer, config, index, scope, serve
from tests import fake_vault

TOKEN = "t" * 40  # test-only placeholder; real tokens live only in the Keychain


def whole_vault_fixture():
    vault, outside, links = fake_vault.build()
    (vault / "00 Inbox").mkdir()
    (vault / "00 Inbox/2026-10-05-0900-capture.md").write_text(
        "---\nclassification: internal\n---\n# Capture\n\nTempo runs happen on Thursdays.\n")
    (vault / "unlabelled.md").write_text("# No frontmatter\n\nUNLABELLED_MARKER tempo.\n")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian/workspace.md").write_text(
        "---\nclassification: internal\n---\nHIDDEN_MARKER tempo.\n")
    return vault


class WholeVaultProfileTest(unittest.TestCase):
    def setUp(self):
        self.vault = whole_vault_fixture()
        self.profile = config.MVP_WHOLE_VAULT

    def test_dot_folders_are_never_listed_or_readable(self):
        listed = ["/".join(p) for p in scope.iter_allowed_files(self.profile, self.vault)]
        self.assertFalse(any(p.startswith(".") or "/." in p for p in listed), listed)
        with self.assertRaises(scope.ScopeError):
            scope.read_text(".obsidian/workspace.md", self.profile, self.vault)

    def test_unlabelled_notes_are_skipped_not_indexed(self):
        skipped = []
        chunks, files = index.build(self.profile, self.vault, skipped=skipped)
        text = "\n".join(c.text for c in chunks)
        self.assertNotIn("UNLABELLED_MARKER", text)
        self.assertEqual(sorted(p for p, _ in skipped), ["unlabelled.md"])
        self.assertNotIn("unlabelled.md", files)

    def test_a_client_note_stops_the_whole_vault_profile(self):
        """AGENTS.md rules 2-3 (Codex, MVP-phone review): stop and flag, never skip."""
        (self.vault / "client-note.md").write_text(
            "---\nclassification: client\n---\n# Client\n\nCLIENT_MARKER tempo.\n")
        with self.assertRaises(index.ProhibitedNote):
            index.build(self.profile, self.vault, skipped=[])

    def test_symlinks_still_never_followed(self):
        chunks, _ = index.build(self.profile, self.vault, skipped=[])
        text = "\n".join(c.text for c in chunks)
        for marker in ("OUTSIDE_VAULT", "DEEP_OUTSIDE"):
            self.assertNotIn(marker, text)

    def test_narrow_profiles_stay_strict(self):
        strict = config.Profile("strict", roots=("",))
        with self.assertRaises(scope.ScopeError):
            index.build(strict, self.vault)


class BindTest(unittest.TestCase):
    def test_only_tailnet_or_loopback(self):
        for ok in ("100.101.102.103", "127.0.0.1", "::1", "fd7a:115c:a1e0::1"):
            self.assertEqual(serve.check_bind(ok), ok)
        for bad in ("0.0.0.0", "::", "", "192.168.1.20", "10.0.0.5", "8.8.8.8", "100.128.0.1",
                    "localhost"):
            with self.assertRaises(serve.BindRefused, msg=bad):
                serve.check_bind(bad)

    def test_build_server_refuses_a_lan_address(self):
        svc = serve.Service(TOKEN, model_factory=adapter.StubAdapter)
        with self.assertRaises(serve.BindRefused):
            serve.build_server("0.0.0.0", 0, svc)


class EndpointFixture(unittest.TestCase):
    """A live /ask server on loopback with a stub model; no tests of its own."""

    def setUp(self):
        # Watchdog: a test that hangs (setup, body or teardown) dumps every thread's stack
        # and exits non-zero after 60 s instead of hanging the suite.
        faulthandler.dump_traceback_later(60, exit=True)
        self.addCleanup(faulthandler.cancel_dump_traceback_later)
        self.vault = whole_vault_fixture()
        self.artifacts = Path(self.vault.parent / (self.vault.name + "-artifacts"))
        self._saved = config.LOGS
        config.LOGS = self.artifacts
        self.model = adapter.StubAdapter(reply="Tempo means marathon pace or faster [E1]")
        self.service = serve.Service(TOKEN, model_factory=lambda: self.model,
                                     vault=self.vault, model_health=lambda: "up")
        self.server = serve.build_server("127.0.0.1", 0, self.service)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        config.LOGS = self._saved

    def request(self, method, path, body=None, token=TOKEN, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        out = resp.status, json.loads(resp.read().decode() or "{}")
        conn.close()
        return out


class AskEndpointTest(EndpointFixture):
    def test_an_answer_comes_back_with_citations_status_and_latency(self):
        code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertEqual(code, 200)
        self.assertIn(body["status"], ("incomplete", "withheld"))  # never `supported` (Q21)
        self.assertTrue(body["citations"])
        for c in body["citations"]:
            self.assertIn("folder", c)
            self.assertRegex(c["lines"], r"^\d+-\d+$")
        self.assertIn("latency_s", body)
        self.assertIn("Not verified", body["display"])

    def test_inbox_citations_are_marked_unreviewed(self):
        self.model.reply = "Tempo runs happen on Thursdays [E1] [E2] [E3] [E4]"
        code, body = self.request("POST", "/ask", {"question": "When do tempo runs happen Thursdays?"})
        self.assertEqual(code, 200)
        inbox = [c for c in body["citations"] if c["folder"] == "00 Inbox"]
        self.assertTrue(inbox, body["citations"])
        self.assertTrue(all(c["unreviewed"] for c in inbox))
        self.assertIn("unreviewed Inbox note", body["display"])

    def test_wrong_or_missing_token_is_401(self):
        for tok in ("x" * 40, TOKEN[:-1], "", None):
            code, body = self.request("POST", "/ask", {"question": "tempo?"}, token=tok)
            self.assertEqual(code, 401, tok)
        self.assertEqual(self.model.calls, [])

    def test_health_needs_no_token_and_shows_no_vault_content(self):
        code, body = self.request("GET", "/health", token=None)
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"ok", "model", "busy"})

    def test_bad_inputs_are_refused_before_the_model(self):
        cases = [
            ({"question": ""}, 400), ({"question": "x" * 501}, 400), ({"question": 42}, 400),
            ({"q": "tempo"}, 400), ({"question": "a\x00b"}, 400),
        ]
        for body, want in cases:
            self.assertEqual(self.request("POST", "/ask", body)[0], want, body)
        self.assertEqual(self.request("POST", "/ask", raw=b"not json")[0], 400)
        self.assertEqual(self.request("POST", "/ask", raw=b"{" + b" " * 5000 + b"}")[0], 413)
        self.assertEqual(self.model.calls, [])

    def test_a_second_concurrent_question_gets_429(self):
        self.service.busy.acquire()
        try:
            code, body = self.request("POST", "/ask", {"question": "tempo?"})
        finally:
            self.service.busy.release()
        self.assertEqual(code, 429)

    def test_a_malformed_model_reply_is_reported_and_traced(self):
        class Broken(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                raise adapter.MalformedResponse("generation did not finish")
        self.service.model_factory = Broken
        code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertEqual((code, body["status"]), (200, "malformed"))
        traces = list(self.artifacts.glob("traces-*.jsonl"))
        self.assertEqual(len(traces), 1)
        record = json.loads(traces[0].read_text().splitlines()[-1])
        self.assertEqual(record["status"], answer.FAILED)
        self.assertIn("unlabelled.md", record["files_read_but_skipped"])

    def test_a_model_that_is_down_gives_503_and_no_fallback(self):
        self.service.model_factory = lambda: adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:9", timeout=2)
        code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertEqual((code, body["status"]), (503, "unavailable"))

    def test_nothing_in_the_vault_changes(self):
        before = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file() and not p.is_symlink()}
        self.request("POST", "/ask", {"question": "What does tempo mean?"})
        after = {p: p.read_bytes() for p in self.vault.rglob("*") if p.is_file() and not p.is_symlink()}
        self.assertEqual(before, after)

    def test_the_token_never_appears_in_traces_or_responses(self):
        code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertNotIn(TOKEN, json.dumps(body))
        for f in self.artifacts.rglob("*"):
            if f.is_file():
                self.assertNotIn(TOKEN, f.read_text())

    def test_accept_never_blocks_the_serving_loop(self):
        # The blocking scenario suspected behind the P2.4 test hang (not proven): the
        # selector says "ready" but no connection is there. accept() on
        # the listening socket must time out, not block, so shutdown() is always reached.
        self.assertEqual(self.server.socket.gettimeout(), serve._Server.ACCEPT_TIMEOUT)
        probe = serve.build_server("127.0.0.1", 0, self.service)
        try:
            started = time.monotonic()
            probe._handle_request_noblock()   # nothing is connecting
            self.assertLess(time.monotonic() - started, 5)
        finally:
            probe.server_close()

    def test_a_peer_outside_the_tailnet_is_refused(self):
        self.assertFalse(serve.allowed_address("192.168.1.30"))
        self.assertFalse(serve.allowed_address("::ffff:192.168.1.30"))
        self.assertTrue(serve.allowed_address("::ffff:100.100.1.2"))


class AcceptanceFixesTest(AskEndpointTest):
    """David's MVP acceptance run, 5 Oct: the four display and error fixes."""

    def test_status_line_is_plain_language(self):
        """Fix 1: no engineering jargon on the phone."""
        self.model.reply = "Tempo runs happen on Thursdays [E1]"
        _, body = self.request("POST", "/ask", {"question": "When do tempo runs happen Thursdays?"})
        last_lines = body["display"].splitlines()[-1]  # latency hidden under 8 s (P2.8 B1)
        for jargon in ("lexical", "entailment", "overlap", "%", "verifier", "incomplete"):
            self.assertNotIn(jargon, last_lines)
        self.assertTrue(last_lines.startswith("Not verified"))
        self.assertEqual(body["status_line"], last_lines)
        self.assertIn("reason", body)  # the technical reason stays in the JSON

    def test_source_lines_are_quoted_verbatim_under_the_answer(self):
        """Fix 2: the note's own words sit under the answer, so a contradiction shows. Since
        P2.8 B1 one quoted line per source, under the note's title (David, Q109)."""
        self.model.reply = "Tempo runs happen on Thursdays [E1]"
        _, body = self.request("POST", "/ask", {"question": "When do tempo runs happen Thursdays?"})
        self.assertIn("“Tempo runs happen on Thursdays.”", body["display"])
        quoted = [q for c in body["citations"] for q in c["quote"]]
        self.assertIn("Tempo runs happen on Thursdays.", quoted)

    def test_quote_lines_picks_the_matching_row_and_skips_headings(self):
        text = "# Logistics\n| Day | Note |\n|---|---|\n- November: book by Fri 16 Oct 2026.\n- Refund only with a certificate.\n"
        self.assertEqual(serve.quote_lines(text, "Book November lunches by Fri 16 Oct [E1]"),
                         ["- November: book by Fri 16 Oct 2026."])
        self.assertEqual(serve.quote_lines(text, "Nothing in common [E1]"), [])

    def test_answer_ids_are_renumbered_to_the_listed_sources(self):
        self.model.reply = "Tempo runs happen on Thursdays [E1] [E9]"
        _, body = self.request("POST", "/ask", {"question": "When do tempo runs happen Thursdays?"})
        self.assertNotRegex(body["answer"], r"\[E\d+\]")
        self.assertIn("[1]", body["answer"])
        self.assertEqual(len(body["citations"]), 1)

    def test_the_mvp_prompt_asks_for_fact_first_and_p22_prompt_is_unchanged(self):
        """Fix 3: fact first, no yes/no lead-in; P2.2's pinned prompt version untouched."""
        self.request("POST", "/ask", {"question": "What does tempo mean?"})
        sent = self.model.calls[-1]
        self.assertIn("Start with the fact itself", sent)
        self.assertIn("Do not begin with Yes or No", sent)
        self.assertNotIn("Do not begin with Yes or No", answer.PROMPT)
        self.assertEqual(adapter.request_signature(answer.PROMPT), "3e00bced79dc")

    def test_every_error_carries_a_readable_display(self):
        """Fix 4: the wrong-token run showed a blank screen."""
        cases = [
            (self.request("POST", "/ask", {"question": "x"}, token="w" * 40), 401),
            (self.request("POST", "/ask", {"question": ""}), 400),
            (self.request("POST", "/ask", raw=b"{" + b" " * 5000 + b"}"), 413),
            (self.request("POST", "/nope", {"question": "x"}), 404),
            (self.request("GET", "/nope"), 404),
        ]
        self.service.busy.acquire()
        try:
            cases.append((self.request("POST", "/ask", {"question": "tempo?"}), 429))
        finally:
            self.service.busy.release()
        self.service.model_factory = lambda: adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:9", timeout=2)
        cases.append((self.request("POST", "/ask", {"question": "What does tempo mean?"}), 503))

        def boom():
            raise RuntimeError("unexpected")
        self.service.model_factory = boom
        cases.append((self.request("POST", "/ask", {"question": "What does tempo mean?"}), 500))
        for (code, body), want in cases:
            self.assertEqual(code, want)
            self.assertTrue(body.get("display", "").strip(), (want, body))
            self.assertNotIn(TOKEN, body["display"])
            self.assertNotIn("unexpected", body["display"])  # no internals on the phone
            self.assertEqual(body.get("spoken"), body["display"], want)  # PL1 step 4: voice = error text
        self.assertTrue(cases[0][0][1]["display"].startswith("Not authorised"))

    def test_a_client_note_pauses_the_service_with_a_display(self):
        (self.vault / "client-note.md").write_text(
            "---\nclassification: client\n---\n# Client\n\nCLIENT_MARKER tempo.\n")
        import io, contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertEqual((code, body["status"]), (423, "paused"))
        self.assertTrue(body["display"].startswith("Paused"))
        self.assertEqual(body["spoken"], body["display"])  # PL1 step 4 review 1/1
        self.assertNotIn("CLIENT_MARKER", json.dumps(body))
        self.assertIn("POLICY FLAG", err.getvalue())
        self.assertIn("client-note.md", err.getvalue())
        self.assertNotIn("CLIENT_MARKER", err.getvalue())
        self.assertEqual(self.model.calls, [])

    def test_the_failure_store_never_receives_spoken(self):
        # D74 consult (session 55): the store gets what it got before the voice existed.
        seen = []
        original = self.service.remember
        def spy(answer_id, question, code, payload, reason_code):
            seen.append(set(payload))
            return original(answer_id, question, code, payload, reason_code)
        self.service.remember = spy
        self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertTrue(seen)
        self.assertNotIn("spoken", seen[0])

    def test_403_off_tailnet_has_a_display(self):
        self.assertIn("Tailscale", serve.error_display(403))


class DeadlineTest(EndpointFixture):
    """Step 5: one end-to-end deadline per request, whatever stage is still running."""

    def setUp(self):
        super().setUp()
        self.release = threading.Event()
        self.entered = threading.Event()
        release, entered = self.release, self.entered

        class Slow(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                entered.set()
                release.wait(20)
                return super().generate(prompt, max_tokens)
        self.service.model_factory = lambda: Slow(reply="Tempo means marathon pace [E1]")
        self.service.deadline = 0.5

    def tearDown(self):
        self.release.set()
        super().tearDown()

    def test_a_slow_answer_gets_504_with_a_display(self):
        code, body = self.request("POST", "/ask", {"question": "What does tempo mean?"})
        self.assertEqual((code, body["status"]), (504, "timeout"))
        self.assertTrue(body["display"].startswith("No answer within"))

    def test_the_late_call_keeps_the_lock_until_it_really_ends(self):
        self.assertEqual(self.request("POST", "/ask", {"question": "What does tempo mean?"})[0], 504)
        self.assertTrue(self.entered.is_set())
        # The model call is still running: a new question must not overlap it.
        self.assertEqual(self.request("POST", "/ask", {"question": "What does tempo mean?"})[0], 429)
        self.release.set()
        for _ in range(100):
            if not self.service.busy.locked():
                break
            threading.Event().wait(0.05)
        self.assertFalse(self.service.busy.locked())
        self.service.deadline = 10
        self.assertEqual(self.request("POST", "/ask", {"question": "What does tempo mean?"})[0], 200)

    def test_the_deadline_counts_from_arrival(self):
        """Time already spent before the model call counts against the same 120 s."""
        import time
        self.service.deadline = 3
        self.release.set()  # the model would answer at once
        self.service.busy.acquire()  # ask_within expects the caller to hold the lock
        code, body = self.service.ask_within("What does tempo mean?", time.monotonic() - 5)
        self.assertEqual(code, 504)


class ServiceConfigTest(unittest.TestCase):
    def test_a_short_token_is_refused(self):
        with self.assertRaises(ValueError):
            serve.Service("short", model_factory=adapter.StubAdapter)


if __name__ == "__main__":
    unittest.main()


class DateAwarenessTest(unittest.TestCase):
    """Q46 priority 2 (David's "menu tomorrow" failure, 5 Oct): relative dates resolved
    against a fixed test date before retrieval, today's date in the prompt, the answer's
    date stated. Fixed date: Mon 5 Oct 2026."""

    TODAY = __import__("datetime").date(2026, 10, 5)

    def test_cowork_regression_phrases_resolve_against_the_test_date(self):
        cases = {
            "What's on the menu tomorrow?": "Tue 6 Oct 2026",
            "Is there climbing next Monday?": "Mon 12 Oct 2026",
            "What's on this Saturday?": "Sat 10 Oct 2026",
            "Bins this week?": "Mon 5 Oct 2026 to Sun 11 Oct 2026",
            "Menu demain ?": "Tue 6 Oct 2026",
            "Anything this weekend?": "Sat 10 Oct 2026",
        }
        for q, expected in cases.items():
            asked, found = serve.resolve_dates(q, self.TODAY)
            self.assertIn(expected, asked, q)
            self.assertTrue(found, q)

    def test_explicit_dates_are_left_as_written_but_recognised(self):
        for q in ("Is there school on Mon 2 Nov?", "Climbing Monday 2 Nov?"):
            asked, found = serve.resolve_dates(q, self.TODAY)
            self.assertEqual(asked, q)
            self.assertEqual([serve.fmt_day(d) for _, ds in found for d in ds], ["Mon 2 Nov 2026"], q)
        self.assertEqual(serve.resolve_dates("What does tempo mean?", self.TODAY),
                         ("What does tempo mean?", []))

    def test_round5_date_reproducers(self):
        """Codex round 5, finding 2: a weekday tied to a week, and a weekday followed by a
        month name, each name one date."""
        cases = {
            "Thursday next week?": ["Thu 15 Oct 2026"],
            "Monday, October 12?": ["Mon 12 Oct 2026"],
            "jeudi de la semaine prochaine ?": ["Thu 15 Oct 2026"],
            "Friday this week?": ["Fri 9 Oct 2026"],
            "le 2 novembre ?": ["Mon 2 Nov 2026"],
            "Anything on 2 Jan?": ["Sat 2 Jan 2027"],  # six months back would be odd: next year
            "Tomorrow, 5 people?": ["Tue 6 Oct 2026"],
        }
        for q, expected in cases.items():
            _, found = serve.resolve_dates(q, self.TODAY)
            self.assertEqual([serve.fmt_day(d) for _, ds in found for d in ds], expected, q)
        self.assertEqual(serve.resolve_dates("What on 31 Feb?", self.TODAY)[1], [("31 Feb", [])])

    def test_a_week_lists_each_day_for_retrieval(self):
        asked, _ = serve.resolve_dates("Bins this week?", self.TODAY)
        self.assertIn("Tue 6 Oct", asked)  # the waste table writes "Tue 6 Oct"
        self.assertIn("Sun 11 Oct", asked)

    def test_the_lunch_table_form_is_searchable(self):
        asked, _ = serve.resolve_dates("menu tomorrow", self.TODAY)
        self.assertIn("Tue 06", asked)  # the menu table writes days as "Tue 06"

    def _service(self, reply):
        vault = whole_vault_fixture()
        (vault / "00 Inbox/2026-10-05-1229-menus.md").write_text(
            "---\nclassification: internal\n---\n# Lunch menus\n\n## October 2026\n"
            "| Day | Main |\n|---|---|\n| Mon 05 | DISH_A |\n| Tue 06 | DISH_B |\n\n"
            "## November 2026\n| Day | Main |\n|---|---|\n| Mon 02 | no school |\n")
        model = adapter.StubAdapter(reply=reply)
        svc = serve.Service(TOKEN, model_factory=lambda: model, vault=vault,
                            tracing=False, today=lambda: self.TODAY)
        return svc, model

    def test_the_model_is_told_today_and_the_date_asked_about(self):
        svc, model = self._service("Tue 6 Oct: DISH_B [E1]")
        code, body = svc.ask("What's on the menu tomorrow?")
        self.assertEqual(code, 200)
        prompt = model.calls[0]
        self.assertIn("Today is Mon 5 Oct 2026", prompt)
        self.assertIn("The question is about Tue 6 Oct 2026", prompt)
        self.assertIn("Nothing found for Tue 6 Oct 2026", prompt)
        self.assertIn("DISH_B", prompt)  # retrieval found the menu table
        self.assertTrue(body["answer"].startswith("(Asked about Tue 6 Oct 2026.) Tue 6 Oct"), body["answer"])

    def test_an_answer_that_omits_the_date_gets_it_stated(self):
        svc, _ = self._service("There is no school on Nov 2. [E1]")
        _, body = svc.ask("What's on the menu tomorrow?")
        self.assertTrue(body["answer"].startswith("(Asked about Tue 6 Oct 2026.)"), body["answer"])
        self.assertTrue(body["display"].startswith("Tue 6 Oct 2026: There is no school"),
                        body["display"])

    def test_no_relative_date_means_no_date_rule(self):
        svc, model = self._service("Tempo is marathon pace [E1]")
        svc.ask("What does tempo mean?")
        self.assertIn("Today is Mon 5 Oct 2026", model.calls[0])
        self.assertNotIn("The question is about", model.calls[0])

    def test_an_answer_about_another_date_than_an_explicit_one_is_marked(self):
        """Codex round 5, finding 3."""
        svc, _ = self._service("There is no school on Tue 6 Oct 2026. [E1]")
        _, body = svc.ask("Is there school on Mon 2 Nov?")
        self.assertTrue(body["answer"].startswith("(Asked about Mon 2 Nov 2026.)"), body["answer"])

    def test_the_wrong_year_does_not_count_as_the_date(self):
        """Codex round 5, finding 4."""
        svc, _ = self._service("Tue 6 Oct 2027: DISH_B [E1]")
        _, body = svc.ask("What's on the menu tomorrow?")
        self.assertTrue(body["answer"].startswith("(Asked about Tue 6 Oct 2026.)"), body["answer"])


class BodyDeadlineTest(EndpointFixture):
    """Codex round 5, finding 5: the deadline also covers reading the request body."""

    def test_a_slow_body_gets_504_within_the_deadline(self):
        import socket as socket_mod
        import time
        self.service.deadline = 0.5
        body = json.dumps({"question": "What does tempo mean?"}).encode()
        s = socket_mod.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(("POST /ask HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                       "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                       % (TOKEN, len(body))).encode() + body[:5])
            started = time.monotonic()
            reply = b""  # read to the end: one recv() may stop after the headers
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                reply += chunk
            reply = reply.decode()
            elapsed = time.monotonic() - started
        finally:
            s.close()
        self.assertIn(" 504 ", reply.split("\r\n", 1)[0])
        self.assertIn("No answer within", reply)
        self.assertLess(elapsed, 5)
        self.assertEqual(self.model.calls, [])

    def test_a_request_already_past_its_deadline_is_not_answered(self):
        import time
        self.service.deadline = 3
        self.service.busy.acquire()
        code, _ = self.service.ask_within("What does tempo mean?", time.monotonic() - 5)
        self.assertEqual(code, 504)
        self.assertFalse(self.service.busy.locked())


class HealthGuardTest(unittest.TestCase):
    """Codex round 5, finding 6: the health check has inference's loopback and redirect guards."""

    def test_a_non_loopback_endpoint_is_down_without_a_request(self):
        self.assertEqual(serve.llama_health("http://example.com:80"), "down")

    def test_a_redirect_is_not_followed(self):
        from http.server import BaseHTTPRequestHandler as H, HTTPServer
        hits = []

        class Redirect(H):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/ok" % self.server.server_address[1])
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = HTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(serve.llama_health("http://127.0.0.1:%d" % srv.server_address[1]), "down")
            self.assertEqual(hits, ["/health"])  # the redirect target was never requested
        finally:
            srv.shutdown()
            srv.server_close()


class Round6Test(EndpointFixture):
    """Codex round 6 reproducers."""

    TODAY = __import__("datetime").date(2026, 10, 5)

    def test_a_curly_apostrophe_reads_like_a_straight_one(self):
        _, found = serve.resolve_dates("next week’s Thursday?", self.TODAY)
        self.assertEqual([serve.fmt_day(d) for _, ds in found for d in ds], ["Thu 15 Oct 2026"])

    def _logged(self, send):
        import io
        import sys as sys_mod
        saved, buf = sys_mod.stderr, io.StringIO()
        sys_mod.stderr = buf
        try:
            send()
        finally:
            sys_mod.stderr = saved
        return buf.getvalue()

    def test_a_token_in_the_url_or_a_bad_request_line_is_never_logged(self):
        import socket as socket_mod
        secret = "SYNTHETIC" + "X" * 30
        log = self._logged(lambda: self.request("POST", "/ask?token=" + secret,
                                                {"question": "What does tempo mean?"}))
        self.assertNotIn(secret, log)
        self.assertIn("POST /ask response 404", log)  # the route without its query string

        def bad_line():
            s = socket_mod.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                s.sendall(("BROKEN %s junk junk\r\n\r\n" % secret).encode())
                s.recv(65536)
            finally:
                s.close()
        self.assertNotIn(secret, self._logged(bad_line))

    def test_headers_count_against_the_deadline(self):
        import socket as socket_mod
        import time
        self.service.deadline = 0.5
        body = json.dumps({"question": "What does tempo mean?"}).encode()
        head = ("POST /ask HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"
                "Content-Type: application/json\r\nContent-Length: %d\r\n" % (TOKEN, len(body))).encode()
        # Complete headers that arrive after the deadline: 504, no model call.
        s = socket_mod.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(head[:20])
            time.sleep(0.3)
            s.sendall(head[20:])
            time.sleep(0.3)
            s.sendall(b"\r\n" + body)
            reply = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                reply += chunk
        except OSError:
            reply = reply if "reply" in dir() else b""
        finally:
            s.close()
        self.assertNotIn(b" 200 ", reply.split(b"\r\n", 1)[0])
        self.assertEqual(self.model.calls, [])

    def test_unfinished_headers_are_dropped_at_the_deadline(self):
        import socket as socket_mod
        import time
        self.service.deadline = 0.5
        s = socket_mod.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(b"POST /ask HTTP/1.1\r\nHost: x\r\n")
            started = time.monotonic()
            data = s.recv(65536)
            elapsed = time.monotonic() - started
        except OSError:
            data, elapsed = b"", 0
        finally:
            s.close()
        self.assertEqual(data, b"")
        self.assertLess(elapsed, 5)
        self.assertEqual(self.model.calls, [])


class DateContractTest(unittest.TestCase):
    """The three-step date reading (session 20, Codex consultation), tested on generated
    combinations rather than the latest reproducer. Fixed date Mon 5 Oct 2026."""

    TODAY = __import__("datetime").date(2026, 10, 5)

    def _days(self, q):
        return [serve.fmt_day(d) for _, ds in serve.resolve_dates(q, self.TODAY)[1] for d in ds]

    def test_every_weekday_and_week_combination_names_one_day(self):
        import datetime
        monday = self.TODAY
        weekdays = {"Monday": 0, "Thursday": 3, "Sunday": 6, "jeudi": 3}
        for wd, n in weekdays.items():
            for week, off in (("next week", 7), ("this week", 0)):
                expected = [serve.fmt_day(monday + datetime.timedelta(off + n))]
                forms = ["%s %s", "on %s %s", "this %s %s", "coming %s %s", "%s, %s", "%s of %s",
                         "%s in %s"]
                for f in forms:
                    q = (f % (wd, week)) + "?"
                    self.assertEqual(self._days(q), expected, q)
                self.assertEqual(self._days("%s's %s?" % (week, wd)), expected)
                self.assertEqual(self._days("%s’s %s?" % (week, wd)), expected)
                self.assertEqual(self._days("%s, %s?" % (week, wd)), expected)
        self.assertEqual(self._days("jeudi de la semaine prochaine ?"), ["Thu 15 Oct 2026"])
        self.assertEqual(self._days("jeudi de cette semaine ?"), ["Thu 8 Oct 2026"])

    def test_more_than_one_date_target_asks_again(self):
        for q in ("Thursday and next week?", "Thursday and Friday next week?",
                  "Thursday of the next week?", "Monday 2 Nov next week?", "Mon 2 Nov and Tue 3 Nov?"):
            found = serve.resolve_dates(q, self.TODAY)[1]
            self.assertEqual(len(found), 1, q)
            self.assertEqual(found[0][1], [], q)

    def test_separate_days_are_never_shown_as_a_range(self):
        import datetime
        label = serve.days_label([datetime.date(2026, 10, 5), datetime.date(2026, 10, 9)])
        self.assertEqual(label, "Mon 5 Oct 2026, Fri 9 Oct 2026")
        self.assertEqual(serve.days_label([datetime.date(2026, 10, 5) + datetime.timedelta(i)
                                           for i in range(7)]),
                         "Mon 5 Oct 2026 to Sun 11 Oct 2026")

    def test_explicit_dates_win_and_keep_their_year(self):
        self.assertEqual(self._days("Monday, October 12?"), ["Mon 12 Oct 2026"])
        self.assertEqual(self._days("2 Nov (2027)?"), ["Tue 2 Nov 2027"])
        self.assertEqual(self._days("October 6,2027?"), ["Wed 6 Oct 2027"])

    def test_unclear_dates_are_reported_not_guessed(self):
        for q in ("What on 31 Feb?", "Monday 31 Feb?", "next Thursday this week?",
                  # round 8 and its class: a modifier next to the phrase, a stray year
                  "last Monday?", "Monday after next?", "the day before Monday?",
                  "since yesterday?", "until Friday?", "in 2025 tomorrow?", "lundi dernier ?"):
            found = serve.resolve_dates(q, self.TODAY)[1]
            self.assertTrue(any(not ds for _, ds in found), q)

    def _service(self, reply):
        vault = whole_vault_fixture()
        model = adapter.StubAdapter(reply=reply)
        return serve.Service(TOKEN, model_factory=lambda: model, vault=vault, tracing=False,
                             today=lambda: self.TODAY), model

    def test_an_unclear_date_asks_again_without_calling_the_model(self):
        svc, model = self._service("whatever [E1]")
        code, body = svc.ask("Is there school on 31 Feb?")
        self.assertEqual((code, body["status"]), (200, "unclear"))
        self.assertIn("Ask again with one plain date", body["display"])
        self.assertEqual(model.calls, [])

    def test_the_asked_date_is_always_stated(self):
        for reply in ("Tue 6 Oct 2026: soup [E1]", "Tue 6 Oct (2100): soup [E1]",
                      "Tue 6 Oct 1899: soup [E1]", "soup [E1]"):
            svc, _ = self._service(reply)
            _, body = svc.ask("menu tomorrow?")
            self.assertTrue(body["answer"].startswith("(Asked about Tue 6 Oct 2026.)"), reply)

    def test_round8_plain_phrases(self):
        cases = {"the day before yesterday?": ["Sat 3 Oct 2026"], "avant-hier ?": ["Sat 3 Oct 2026"],
                 "le week-end ?": ["Sat 10 Oct 2026", "Sun 11 Oct 2026"],
                 "le week-end prochain ?": ["Sat 17 Oct 2026", "Sun 18 Oct 2026"],
                 "the day after tomorrow?": ["Wed 7 Oct 2026"]}
        for q, expected in cases.items():
            self.assertEqual(self._days(q), expected, q)


class SecretNeverEchoedTest(EndpointFixture):
    """A synthetic secret placed in every request field reaches neither the log nor any
    byte of the response (session 20, Codex consultation)."""

    SECRET = "SYNTHSECRET" + "Q" * 30

    def _exchange(self, raw):
        import io
        import socket as socket_mod
        import sys as sys_mod
        saved, buf = sys_mod.stderr, io.StringIO()
        sys_mod.stderr = buf
        try:
            s = socket_mod.create_connection(("127.0.0.1", self.port), timeout=10)
            try:
                s.sendall(raw)
                reply = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
            except OSError:
                pass
            finally:
                s.close()
            threading.Event().wait(0.2)  # let the handler finish logging
        finally:
            sys_mod.stderr = saved
        return buf.getvalue(), reply

    def test_in_every_field(self):
        x = self.SECRET
        body = json.dumps({"question": "What does tempo mean?"}).encode()
        auth = "Authorization: Bearer %s\r\n" % TOKEN
        cases = [
            ("%s /ask HTTP/1.1\r\nHost: h\r\n\r\n" % x).encode(),                       # method
            ("GET /%s HTTP/1.1\r\nHost: h\r\n\r\n" % x).encode(),                       # path
            ("GET /health?t=%s HTTP/1.1\r\nHost: h\r\n\r\n" % x).encode(),              # query
            ("GET /health HTTP/%s\r\nHost: h\r\n\r\n" % x).encode(),                    # version
            ("BROKEN %s junk junk\r\n\r\n" % x).encode(),                               # bad line
            ("POST /ask HTTP/1.1\r\nHost: h\r\nAuthorization: Bearer %s\r\n"
             "Content-Length: %d\r\n\r\n" % (x, len(body))).encode() + body,           # bad token
            ("POST /ask HTTP/1.1\r\nHost: h\r\n%sX-%s: 1\r\nContent-Length: %d\r\n\r\n"
             % (auth, x, len(body))).encode() + body,                                   # header name
            ("POST /ask HTTP/1.1\r\nHost: h\r\n%sContent-Length: %s\r\n\r\n"
             % (auth, x)).encode(),                                                     # bad length
            ("POST /ask HTTP/1.1\r\nHost: h\r\n%sContent-Length: 60\r\n\r\n{\"q\": \"%s\"}"
             % (auth, x[:40])).encode(),                                                # bad body
        ]
        for raw in cases:
            log, reply = self._exchange(raw)
            self.assertNotIn(x[:20], log, raw[:40])
            self.assertNotIn(x[:20].encode(), reply, raw[:40])


class FailClosedDateTest(unittest.TestCase):
    """Fail-closed date reading (session 31, after review round 9 and a Codex consultation):
    the supported phrases are read exactly as before; any other date-like text makes the
    question unclear. The oracle below is the test's own vocabulary and arithmetic, not the
    service's: a date is never silently dropped or rewritten."""

    TODAY = __import__("datetime").date(2026, 10, 5)  # Mon 5 Oct 2026
    REFS = [__import__("datetime").date(*d) for d in
            ((2026, 10, 5), (2026, 10, 11), (2026, 12, 31), (2028, 2, 28))]  # Mon, Sun, year end, leap

    def _found(self, q, today=None):
        return serve.resolve_dates(q, today or self.TODAY)[1]

    def _unclear(self, q, today=None):
        found = self._found(q, today)
        return len(found) == 1 and found[0][1] == []

    def test_round9_reproducers_are_unclear(self):
        for q in ("Climbing October 6–8?", "Climbing October 6-8?", "School 6 to 8 October?",
                  "next next Monday?", "the previous week's Monday?", "Menu on 2026-10-06?",
                  "Menu on 2026-10-06"):
            self.assertTrue(self._unclear(q), q)

    def test_consultation_cases_are_unclear(self):
        for q in ("Menu in 2 days?", "Menu dans deux jours ?", "Poubelles ce soir ?",
                  "Climbing tonight?", "School on the sixth?", "Menu on 6.10?", "Menu on 6 / 10?",
                  "November tomorrow?", "Climbing in March next Monday?", "October 6, 8 for school?",
                  "Climbing 6 through to 8 October?", "Not this Monday?", "next school Monday?",
                  "lundi 6 octobre ?", "Monday week?", "a week on Monday?", "mid-October?",
                  "mi-octobre ?", "last October?", "end of October?", "early Nov?",
                  "Anything on the weekend?", "every Monday?", "Is there climbing on Tue?",
                  "Thurs climbing?", "Climbing on Mon?", "Lunch menu:2026-10-06?", "Oct6 menu?",
                  "menu tomorow?", "wenesday climbing?", "Menu le trois ?", "ce midi ?",
                  "Is there school on Halloween?", "Lunch menu for May?", "Menu May?", "climbing jan?", "Menus for Oct?",
                  "Climbing in March?", "When is the deadline to book November school hot lunches?",
                  "Monday the 12th?", "on the 12?", "last Monday?", "in two weeks?",
                  "this morning?", "Monday 31 Feb?", "in 2027?", "the week after next?"):
            self.assertTrue(self._unclear(q), q)

    def test_supported_forms_still_resolve(self):
        cases = {"What's on the menu tomorrow?": ["Tue 6 Oct 2026"],
                 "Is there climbing next Monday?": ["Mon 12 Oct 2026"],
                 "School lunch Thursday?": ["Thu 8 Oct 2026"],
                 "lundi 5 octobre ?": ["Mon 5 Oct 2026"],
                 "Tomorrow, 5 people?": ["Tue 6 Oct 2026"],
                 "Climbing Monday at 18:00?": ["Mon 5 Oct 2026"],
                 "climbing tomorrow evening?": ["Tue 6 Oct 2026"],
                 "When do tempo runs happen Thursdays?": []}
        for q, expected in cases.items():
            self.assertEqual([serve.fmt_day(d) for _, ds in self._found(q) for d in ds], expected, q)
        for q in ("What does tempo mean?", "What is D53?", "P2.4 status?",
                  "What was my chip time at the river race?"):
            self.assertEqual(self._found(q), [], q)

    def test_with_no_date_read_any_date_pointer_asks_again(self):
        """Review round 13: with no phrase read there is no "(Asked about ...)" label, so a
        word that may point at a date makes the question unclear. Over-asking is accepted."""
        for q in ("Lunch menu for the next weekday?", "Lunch menu on New Year's Day?",
                  "What's next?", "Which day is climbing?", "Which week is the break?",
                  "menu le lendemain ?", "Climbing on my birthday?", "the eve of the race?",
                  "dans un an ?", "Lunch menu on the holiday?", "Climbing the following one?",
                  "School on Christmas?", "menus de la veille ?", "Bins on collection day?",
                  # red-team pass before round 14, one per class
                  "menu tmrw", "cantine auj", "menu demian", "escalade sam ?", "menu de ven",
                  "Weds?", "this wk", "poubelles cette sem", "scouts le w-e",
                  "menu after half term", "pendant les vacances ?", "la rentrée ?",
                  "is there school during the strike", "Saint Nicolas ?", "Sinterklaas?",
                  "fête du roi ?", "la Saint-Valentin ?", "menu morgen", "maandag?",
                  "what's for lunch right now", "climbing in an hour", "the twenty first?",
                  "menu W42",
                  # round 14: a low ordinal followed by polite wording
                  "Lunch menu on the first, please?", "Lunch menu for the second please?",
                  "Lunch menu on the third, thanks?", "menu le premier, merci ?",
                  # run B: a low ordinal followed by any wording
                  "Lunch menu on the first if possible?", "Climbing on the second or not?",
                  "menu le premier si possible ?", "Bins the third as usual?",
                  # run B review: seasons
                  "What is on the school lunch menu this winter?", "Climbing in summer?",
                  "menu in autumn?", "Bins this fall?", "escalade cet été ?", "menu au printemps ?",
                  "cantine en hiver ?", "scouts cette saison ?"):
            self.assertTrue(self._unclear(q), q)
        for q in ("Le menu a été changé ?", "Les scouts ont été annulés ?"):
            self.assertEqual(self._found(q), [], q)
        for q in ("What is the second brain?", "Status of the second brain project?"):
            self.assertFalse(self._unclear(q), q)
            self.assertEqual(self._found(q), [], q)

    # --- generated text -------------------------------------------------------------
    WD = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5,
          "Sunday": 6, "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
          "samedi": 5, "dimanche": 6}

    def _supported(self, today):
        """(phrase, [expected dates]) computed by the test's own arithmetic."""
        import datetime
        day = datetime.timedelta
        mon = today - day(today.weekday())
        out = [("tomorrow", [today + day(1)]), ("demain", [today + day(1)]),
               ("today", [today]), ("yesterday", [today - day(1)]), ("hier", [today - day(1)]),
               ("the day after tomorrow", [today + day(2)]), ("après-demain", [today + day(2)]),
               ("this weekend", [mon + day(5), mon + day(6)]),
               ("next weekend", [mon + day(12), mon + day(13)]),
               ("this week", [mon + day(i) for i in range(7)]),
               ("next week", [mon + day(7 + i) for i in range(7)]),
               ("la semaine prochaine", [mon + day(7 + i) for i in range(7)])]
        for w, n in self.WD.items():
            coming = today + day((n - today.weekday()) % 7)
            strictly = today + day((n - today.weekday() - 1) % 7 + 1)
            out += [(w, [coming]), ("on " + w, [coming]),
                    ("%s %s" % ("next" if w[0].isupper() else w, w if w[0].isupper() else "prochain"),
                     [strictly]),
                    ("%s next week" % w if w[0].isupper() else "%s de la semaine prochaine" % w,
                     [mon + day(7 + n)])]
        d = today + day(40)
        months = ("January", "February", "March", "April", "May", "June", "July", "August",
                  "September", "October", "November", "December")
        out += [("%d %s %d" % (d.day, months[d.month - 1], d.year), [d]),
                ("%s %d, %d" % (months[d.month - 1], d.day, d.year), [d])]
        return out

    PREFIX = ("", "What's on the menu ", "Is there climbing ", "School lunch ", "Bins ",
              "Les menus ", "Escalade ", "Any plans ")
    SUFFIX = ("?", " ?", " please?", " for the kids?", ", 5 people?", "")
    # Date-like insertions, written for this test (not the service's lists): each one
    # obscures or changes the date, so the result must be unclear.
    BEFORE = ("next", "last", "previous", "every", "not", "the week after", "2", "the 3rd",
              "October", "lundi", "mid", "early", "6 to", "6 -", "6–", "from", "until", "before",
              "after", "since", "dernier", "in 2 days", "2027", "Oct", "the 12th", "next school",
              "a week on", "every other", "the sixth of")
    AFTER = ("to Friday", "- 8", "–8", ", 8", "and 9 Oct", "or Tuesday", "2027", "lundi", "weeks",
             "the 12th", "6.10", "2026-10-06", "ago", "after next", "in 2 days", "ce soir", "tonight",
             "week", "dernier", "6 / 10", "until Friday", "mid-November", "the third", "3e",
             "1er", "15/10", "and the next day", "fortnight", "the following week")

    def test_generated_supported_text_resolves_to_the_right_dates(self):
        import random
        rng = random.Random(31)
        for today in self.REFS:
            for phrase, expected in self._supported(today):
                for _ in range(6):
                    q = rng.choice(self.PREFIX) + phrase + rng.choice(self.SUFFIX)
                    found = self._found(q, today)
                    self.assertEqual(len(found), 1, (today, q, found))
                    self.assertEqual(sorted(found[0][1]), sorted(expected), (today, q))

    def test_generated_date_like_insertions_are_unclear(self):
        import random
        rng = random.Random(9)
        for today in self.REFS:
            for phrase, _ in self._supported(today):
                for _ in range(8):
                    pre, post = rng.choice(self.PREFIX), rng.choice(self.SUFFIX)
                    before = rng.choice(self.BEFORE)
                    after = rng.choice(self.AFTER)
                    if before == "next" and phrase in self.WD:
                        before = "last"  # "next" + a bare weekday is itself a supported phrase
                    if phrase.endswith(("week", "semaine prochaine")):  # weekday + week composes
                        before = "last" if before == "lundi" else before
                        after = "weeks" if after == "lundi" else after
                    if rng.random() < 0.5:
                        q = pre + before + " " + phrase + post
                    else:
                        q = pre + phrase + " " + after + post
                    self.assertTrue(self._unclear(q, today), (today, q, self._found(q, today)))

    def test_random_text_never_drops_a_date(self):
        """Random sequences of date words, digits, dashes, "to", qualifiers and fillers, EN
        and FR: either unclear, or one phrase that contains every date-like token, or no
        date and no date-like token at all."""
        import random
        rng = random.Random(5)
        dateish = ("Monday", "jeudi", "October", "novembre", "Oct", "6", "12", "2027", "3rd",
                   "next", "last", "previous", "prochain", "dernier", "week", "weeks", "semaine",
                   "weekend", "tomorrow", "demain", "today", "–", "-", "to", "au", "/", "every",
                   "2026-10-06", "this", "on", "mid", "tonight", "days")
        strong = {"Monday", "jeudi", "October", "novembre", "Oct", "6", "12", "2027", "3rd",
                  "next", "last", "previous", "prochain", "dernier", "weeks",
                  "weekend", "tomorrow", "demain", "today", "every", "2026-10-06", "mid",
                  "tonight", "days"}
        # A singular "week" / "semaine" with no qualifier names no date ("Which week is the
        # break?"), so it is not in `strong`; with a qualifier it is, and the parser flags it.
        filler = ("menu", "school", "climbing", "lunch", "bins", "for", "the", "what", "is")
        for n in range(4000):
            words = [rng.choice(dateish if rng.random() < 0.5 else filler)
                     for _ in range(rng.randint(1, 6))]
            q = " ".join(words) + "?"
            found = self._found(q)
            present = [w for w in words if w in strong]
            if not found:  # a qualifier alone ("what's next?") names no date
                qualifiers = {"next", "last", "previous", "prochain", "dernier", "every", "mid"}
                self.assertEqual([w for w in present if w not in qualifiers], [], q)
                continue
            self.assertEqual(len(found), 1, q)
            phrase, days = found[0]
            if days:
                left = (" " + q.replace(phrase, " ", 1) + " ")
                for w in present:
                    self.assertNotIn(" %s " % w, left.replace("?", " "), (q, phrase))

    # --- the service ----------------------------------------------------------------
    def _service(self, reply="Tue 6 Oct 2026: soup [E1]"):
        vault = whole_vault_fixture()
        model = adapter.StubAdapter(reply=reply)
        return serve.Service(TOKEN, model_factory=lambda: model, vault=vault, tracing=False,
                             today=lambda: self.TODAY), model

    def test_unclear_means_no_retrieval_and_no_model_call(self):
        from unittest import mock
        for q in ("School 6 to 8 October?", "next next Monday?", "Menu on 2026-10-06?",
                  "Climbing tonight?"):
            svc, model = self._service()
            with mock.patch.object(serve.answer, "ask") as ask:
                code, body = svc.ask(q)
            self.assertEqual((code, body["status"]), (200, "unclear"), q)
            ask.assert_not_called()
            self.assertEqual(model.calls, [], q)
            self.assertIn("Ask again with one plain date", body["display"], q)

    def test_resolved_dates_are_labelled_in_answer_and_display(self):
        for q, label in (("What's on the menu tomorrow?", "Tue 6 Oct 2026"),
                         ("Is there climbing next Monday?", "Mon 12 Oct 2026"),
                         ("School lunch Thursday?", "Thu 8 Oct 2026")):
            svc, _ = self._service()
            _, body = svc.ask(q)
            self.assertTrue(body["answer"].startswith("(Asked about %s.)" % label), (q, body["answer"]))
            self.assertTrue(body["display"].startswith(label + ": "), (q, body["display"]))

    def test_every_smoke_question_still_reads(self):
        """The real-model smoke questions (experiments/mvp_smoke.py) keep their dates: none
        becomes unclear under the fail-closed rule."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "mvp_smoke", Path(__file__).resolve().parents[1] / "experiments/mvp_smoke.py")
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        for q, _ in smoke.QUESTIONS:
            self.assertFalse(self._unclear(q, smoke.SMOKE_TODAY), q)

    def test_ambiguous_month_and_weekday_words_are_unclear_in_any_position(self):
        """Round 12 and the consultation after it: a word that is also a month or weekday
        abbreviation is date-like wherever it sits. Over-flagging "May I" or "mon menu" is
        accepted (should-fix class)."""
        for w in ("May", "may", "Jan", "Mar", "sept", "Mon", "mon", "Sat", "sun", "Wed", "Oct"):
            for q in ("%s lunch menu?", "Lunch menu %s please?", "Lunch menu %s?",
                      "%s menu tomorrow?", "Menu tomorrow %s?"):
                self.assertTrue(self._unclear(q % w), q % w)
        self.assertEqual([serve.fmt_day(d) for _, ds in self._found("Climbing on 6 May?") for d in ds],
                         ["Wed 6 May 2026"])  # under six months back: this year


class DatedNegationTest(unittest.TestCase):
    """P2.5 F2 on the phone path: the verdict, its reason code, the status line, and the
    row added below the model's answer, which is kept (David, 13 Oct)."""

    class Chunk:
        def __init__(self, text, heading="Choir 2026"):
            self.text, self.heading = text, heading

    ROW = "| Sat 10 Oct | Normal rehearsal | 10:00-12:00 |"

    def result(self, text, status=answer.INCOMPLETE, code=answer.UNVERIFIED):
        hits = [(1.0, self.Chunk("| Date | Event | Time |\n|---|---|---|\n" + self.ROW))]
        return answer.Result("q", status, text, [], hits, reason="r", reason_code=code)

    def test_a_cited_bare_no_becomes_a_defect_and_keeps_the_answer(self):
        for status, code in ((answer.INCOMPLETE, answer.UNVERIFIED),
                             (answer.WITHHELD, answer.DECLINED)):
            r = self.result("Nothing found for Sat 10 Oct 2026 [E1].", status, code)
            self.assertIsNotNone(serve.check_dated_negation(r, [datetime.date(2026, 10, 10)]))
            self.assertEqual((r.status, r.reason_code), (answer.INCOMPLETE, answer.NEGATION_VS_DATED))
            self.assertEqual(r.answer, "Nothing found for Sat 10 Oct 2026 [E1].\n\nBut the note "
                             "it cites lists this for that day: “Sat 10 Oct · Normal rehearsal · "
                             "10:00-12:00” [E1]. Check the note.")
            self.assertEqual(serve.plain_status(r.status, r.reason, []),
                             "Not verified: the answer says nothing is on, but the cited note "
                             "lists that day")

    def test_left_alone_undated_uncited_no_evidence_or_positive(self):
        sat10 = [datetime.date(2026, 10, 10)]
        cases = [(self.result("Nothing found [E1]."), []),
                 (self.result("Nothing found for Sat 10 Oct 2026."), sat10),
                 (self.result("No supporting evidence.", answer.WITHHELD, answer.NO_EVIDENCE), sat10),
                 (self.result("Normal rehearsal, 10:00-12:00 [E1]."), sat10),
                 (self.result("Nothing found [E7]."), sat10),
                 (self.result("No rehearsal [E1]."), sat10)]
        for r, days in cases:
            before = (r.status, r.reason_code, r.answer)
            self.assertIsNone(serve.check_dated_negation(r, days))
            self.assertEqual((r.status, r.reason_code, r.answer), before)

    def test_only_exact_offered_ids_count(self):
        """F2 D74 consult (13 Oct): "E01" or "E١" is not E1, and silences the check."""
        for text in ("Nothing found for Sat 10 Oct 2026 [E01].",
                     "Nothing found for Sat 10 Oct 2026 [E\u0661].",
                     "Nothing found for Sat 10 Oct 2026 [E1] [E2]."):
            r = self.result(text)
            self.assertIsNone(serve.check_dated_negation(r, [datetime.date(2026, 10, 10)]), text)
            self.assertEqual(r.answer, text)

    def test_the_answer_is_kept_byte_for_byte(self):
        r = self.result("Nothing found for Sat 10 Oct 2026 [E1].  \n")
        serve.check_dated_negation(r, [datetime.date(2026, 10, 10)])
        self.assertTrue(r.answer.startswith("Nothing found for Sat 10 Oct 2026 [E1].  \n\n"))

    def test_only_the_cited_chunk_gives_the_row_and_the_phone_renumbers_it(self):
        class Cited(self.Chunk):
            def cite(self):
                return "Choir.md:1-3"
        tempting = Cited("| Date | Event | Time |\n|---|---|---|\n"
                         "| Sat 10 Oct | Normal rehearsal | 10:00-12:00 |")
        other = Cited("| Date | Event | Time |\n|---|---|---|\n| Sat 10 Oct | Concert | 20:00 |")
        hits = [(1.0, tempting), (0.9, other)]
        r = answer.Result("q", answer.INCOMPLETE, "Nothing found for Sat 10 Oct 2026 [E2].", [],
                          hits, reason="r", reason_code=answer.UNVERIFIED)
        self.assertEqual(serve.check_dated_negation(r, [datetime.date(2026, 10, 10)]),
                         (2, "| Sat 10 Oct | Concert | 20:00 |"))
        serve.Service._citations(r)
        self.assertEqual(r.answer, "Nothing found for Sat 10 Oct 2026 [1].\n\nBut the note it "
                         "cites lists this for that day: “Sat 10 Oct · Concert · 20:00” [1]. "
                         "Check the note.")
        r = answer.Result("q", answer.INCOMPLETE, "Nothing found for Sat 10 Oct 2026 [E2].", [],
                          hits[:1], reason="r", reason_code=answer.UNVERIFIED)
        self.assertIsNone(serve.check_dated_negation(r, [datetime.date(2026, 10, 10)]))


class AliasExpansionTest(unittest.TestCase):
    """P2.5 F3: the phone appends an alias's note wording to the question before retrieval,
    so a note that never uses the asked word is found (synthetic note and table)."""

    TODAY = datetime.date(2026, 10, 9)

    def _service(self):
        vault = whole_vault_fixture()
        (vault / "20 Areas/Hobbies/hiking.md").parent.mkdir(parents=True, exist_ok=True)
        (vault / "20 Areas/Hobbies/hiking.md").write_text(
            "---\nclassification: internal\n---\n# Hiking club\n\n| Date | Event | Time |\n"
            "|---|---|---|\n| Sat 10 Oct | Normal outing | 14:00-17:30 |\n")
        model = adapter.StubAdapter(reply="Sat 10 Oct: Normal outing, 14:00-17:30 [E1]")
        svc = serve.Service(TOKEN, model_factory=lambda: model, vault=vault,
                            tracing=False, today=lambda: self.TODAY)
        return svc, model

    def test_an_alias_finds_the_note_that_never_uses_the_word(self):
        svc, model = self._service()
        svc.aliases = {("trailblazers",): ("hiking", "outing")}
        svc.ask("Trailblazers?")  # undated: no date form can find the note instead
        self.assertIn("(also: hiking, outing)", model.calls[0])
        self.assertIn("Normal outing", model.calls[0])  # retrieval found the hiking note

    def test_without_the_alias_the_note_is_not_found(self):
        svc, model = self._service()
        svc.aliases = {}
        _, body = svc.ask("Trailblazers?")
        self.assertEqual(model.calls, [])  # no evidence, so no model call
        self.assertNotIn("(also:", body.get("answer", ""))

    def test_the_service_uses_the_real_table_by_default(self):
        svc, _ = self._service()
        self.assertIs(svc.aliases, __import__("dbrain.aliases").aliases.ALIASES)
