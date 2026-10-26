"""PL1 step 7, append-only correction (D143): "that's wrong" drafts one more Inbox note that
names a note the answer cited; on Save the step 6 writer creates it, and the index leaves
the named note out of answers. No existing file is ever edited.

Every test that writes uses a synthetic temporary vault; nothing here touches the real one.
"""
import datetime
import hashlib
import json
import threading
import unittest

from dbrain import capture, config, index, serve
from tests.test_capture import NOW
from tests.test_serve import EndpointFixture, whole_vault_fixture

TARGET = "00 Inbox/2026-10-05-0900-capture.md"


def sha(path):
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def snapshot(vault):
    return {str(p.relative_to(vault)): p.read_bytes() for p in vault.rglob("*") if p.is_file()}


class TargetTest(unittest.TestCase):
    def test_ordinary_vault_paths_are_accepted(self):
        for p in (TARGET, "10 Projects/AI Second Brain/PROGRESS.md", "20 Areas/Running/Tempo.md",
                  "30 Resources/Résumé notes (v2), draft.md", "a.md"):
            self.assertIsNone(capture.check_target(p), p)

    def test_paths_the_frontmatter_or_a_link_could_misread_are_refused(self):
        for p in ("@Plan.md", "-x.md", "/abs.md", "a/../b.md", "a//b.md", "a/.hidden.md",
                  "A|B.md", "a: b.md", "a #b.md", "a#b.md", "[x].md", "a'b.md", 'a"b.md',
                  "a\\b.md", "a\nb.md", "a\tb.md", "note.txt", "note", "", " a.md", "a.md ",
                  "_a.md", "x" * 200 + ".md", None, 3, "Café.md"):
            self.assertEqual(capture.check_target(p), "target", repr(p))

    def test_the_path_goes_through_the_word_lists(self):
        """Codex D129 consult: the path is saved too, so it is guarded like the text."""
        self.assertEqual(capture.check_target("Work/client-workshop.md"), "engagement")
        self.assertEqual(capture.check_target("Family/school run.md"), "child")
        self.assertIsNone(capture.check_target("Work/Product School notes.md"))

    def test_dated_inbox_names_are_not_taken_for_id_numbers(self):
        self.assertIsNone(capture.check_target("00 Inbox/2026-10-22-1405-scaleway-bills.md"))
        self.assertIsNone(capture.check_target("30 Resources/Gemma 4 26B-A4B notes.md"))

    def test_number_and_key_shapes_in_the_path_are_refused(self):
        """Review 1/2 (session 59), blocker: the path is saved, so its shapes are guarded."""
        for p, reason in (("30 Resources/BE68539007547034.md", "id_number"),
                          ("a/123 456 789.md", "id_number"),
                          ("00 Inbox/2026-10-22-1405-123456789.md", "id_number"),
                          ("2026-10-22-1405-x/AB12cd34EF56gh78.md", "id_number"),
                          ("x/Ab1cdEfgHij2klmnoP.md", "secret"),
                          ("x/sk-abcdef.md", "secret")):
            self.assertEqual(capture.check_target(p), reason, p)

    def test_rendered_target_reads_back_exactly_through_the_index_grammar(self):
        for p in (TARGET, "10 Projects/AI Second Brain/PROGRESS.md",
                  "30 Resources/Résumé notes (v2), draft.md", "a - b.md"):
            name, data = capture.render_correction("Tempo is threshold pace", "work", p, NOW)
            raw = data.decode("utf-8")
            self.assertEqual(index._checked_label(raw), "internal")
            self.assertEqual(index.correction_target(raw), p)
            self.assertTrue(capture.NAME.fullmatch(name), name)
            self.assertTrue(data.startswith(b"---\nclassification: internal\n"))

    def test_render_refuses_a_bad_target_or_text(self):
        for args in (("ok", "work", "A|B.md"), ("", "work", TARGET), ("ok", "maybe", TARGET)):
            with self.assertRaises(ValueError):
                capture.render_correction(*args, NOW)


class IndexExclusionTest(unittest.TestCase):
    def setUp(self):
        self.vault = whole_vault_fixture()
        self.profile = config.MVP_WHOLE_VAULT
        self.inbox = self.vault / "00 Inbox"

    def files(self):
        chunks, files = index.build(self.profile, self.vault, skipped=[])
        self.assertEqual(sorted({c.path for c in chunks}), sorted(files))
        self.assertEqual([c.id for c in chunks], ["C%d" % i for i in range(1, len(chunks) + 1)])
        return files

    def write(self, rel, text):
        (self.vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.vault / rel).write_text(text, encoding="utf-8")

    def correction(self, target, extra=""):
        return ("---\nclassification: internal\nnote_type: correction\ncorrects: %s\n%s---\n"
                "# Correction\n\nTempo runs are on Fridays.\n" % (target, extra))

    def test_a_correction_in_the_inbox_hides_its_target_and_is_itself_served(self):
        self.assertIn(TARGET, self.files())
        self.write("00 Inbox/2026-10-22-1405-correction-x.md", self.correction(TARGET))
        files = self.files()
        self.assertNotIn(TARGET, files)
        self.assertIn("00 Inbox/2026-10-22-1405-correction-x.md", files)

    def test_order_of_files_does_not_matter(self):
        """A correction read before or after its target hides it either way."""
        self.write("00 Inbox/0000-a-correction.md", self.correction(TARGET))
        self.write("00 Inbox/zzzz-z-correction.md", self.correction("unlabelled.md"))
        self.assertNotIn(TARGET, self.files())

    def test_only_inbox_correction_notes_hide_anything(self):
        before = set(self.files())
        self.write("10 Projects/c.md", self.correction(TARGET))               # not the Inbox
        self.write("00 Inbox/capture-says.md",                                # not a correction
                   "---\nclassification: internal\nnote_type: capture\ncorrects: %s\n---\nx\n"
                   % TARGET)
        self.write("00 Inbox/body-only.md",                                   # body, not frontmatter
                   "---\nclassification: internal\n---\nnote_type: correction\ncorrects: %s\n"
                   % TARGET)
        self.write("00 Inbox/quoted.md", self.correction('"%s"' % TARGET))    # quoted value
        self.write("00 Inbox/unlabelled-corr.md",                             # fails the label check
                   "---\nnote_type: correction\ncorrects: %s\n---\nx\n" % TARGET)
        files = self.files()
        self.assertIn(TARGET, files)
        self.assertTrue(before <= set(files) | {"unlabelled-corr.md"})

    def test_a_correction_cannot_hide_a_client_note(self):
        """A client note stops the build before any exclusion (AGENTS.md rule 3)."""
        self.write("client.md", "---\nclassification: client\n---\nCLIENT\n")
        self.write("00 Inbox/c.md", self.correction("client.md"))
        with self.assertRaises(index.ProhibitedNote):
            index.build(self.profile, self.vault, skipped=[])

    def test_chains_and_several_corrections(self):
        self.write("00 Inbox/c1.md", self.correction(TARGET))
        self.write("00 Inbox/c2.md", self.correction("00 Inbox/c1.md"))
        files = self.files()
        self.assertNotIn(TARGET, files)        # accepted (D129 consult): stays hidden
        self.assertNotIn("00 Inbox/c1.md", files)
        self.assertIn("00 Inbox/c2.md", files)

    def test_a_correction_naming_a_missing_note_changes_nothing_else(self):
        before = set(self.files())
        self.write("00 Inbox/c.md", self.correction("10 Projects/missing.md"))
        self.assertEqual(set(self.files()), before | {"00 Inbox/c.md"})

    def test_the_phone_profile_serves_the_inbox(self):
        """Corrections work only where the Inbox is served; the phone's profile does."""
        self.assertIn(TARGET, self.files())


class CorrectRouteTest(EndpointFixture):
    def setUp(self):
        super().setUp()
        self.service.now = lambda: NOW
        self.inbox = self.vault / "00 Inbox"
        self.aid = "a" * 32
        self.service.cited.put(self.aid, [(TARGET, sha(self.vault / TARGET)),
                                          ("unlabelled.md", None)])
        self.model.reply = "Tempo runs happen on Fridays."

    def correct(self, text="No, tempo runs are on Fridays now", source="1", domain="work",
                aid=None):
        return self.request("POST", "/correct", {"answer_id": aid or self.aid, "source": source,
                                                 "text": text, "domain": domain})

    def test_draft_then_save_writes_one_correction_and_hides_the_target(self):
        before = snapshot(self.vault)
        code, body = self.correct()
        self.assertEqual((code, body["status"]), (200, "draft"))
        self.assertEqual(body["spoken"],
                         "Work correction for your Inbox: Tempo runs happen on Fridays. The note "
                         "2026-10-05-0900-capture will no longer be used in answers. Say Save to "
                         "keep it.")
        self.assertEqual(snapshot(self.vault), before, "a draft must not write")
        code, saved = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((code, saved["status"]), (200, "saved"))
        name = "2026-10-22-1405-correction-tempo-runs-happen-on-fridays.md"
        self.assertEqual(saved["path"], "00 Inbox/" + name)
        after = snapshot(self.vault)
        self.assertEqual(set(after) - set(before), {"00 Inbox/" + name})
        self.assertEqual({k: v for k, v in after.items() if k in before}, before,
                         "no existing file changed")
        _, data = capture.render_correction("Tempo runs happen on Fridays.", "work", TARGET, NOW)
        self.assertEqual(after["00 Inbox/" + name], data)
        _, files = index.build(self.service.profile, self.vault, skipped=[])
        self.assertNotIn(TARGET, files)
        self.assertIn("00 Inbox/" + name, files)

    def test_the_corrected_note_no_longer_reaches_answers(self):
        _, body = self.correct()
        _, saved = self.request("POST", "/capture/save", {"id": body["id"]})
        self.model.reply = "Answer [E1]"
        self.model.calls.clear()
        _, ans = self.request("POST", "/ask", {"question": "When do tempo runs happen?"})
        self.assertNotIn(TARGET, [c["path"] for c in ans["citations"]])
        self.assertFalse(any("Tempo runs happen on Thursdays" in p for p in self.model.calls))
        # Review 1/2 should-fix: the correction itself reaches the model and the citations.
        self.assertTrue(any("Tempo runs happen on Fridays" in p for p in self.model.calls))
        self.assertEqual(ans["citations"][0]["path"], saved["path"])

    def test_end_to_end_from_an_answer(self):
        self.model.reply = "Tempo runs happen on Thursdays [E1]"
        _, ans = self.request("POST", "/ask", {"question": "When do tempo runs happen?"})
        path = ans["citations"][0]["path"]
        self.model.reply = "Tempo runs happen on Fridays."
        self.assertIsNone(capture.check_target(path), path)
        code, body = self.correct(aid=ans["answer_id"])
        self.assertEqual(body["status"], "draft", body)
        self.request("POST", "/capture/save", {"id": body["id"]})
        _, files = index.build(self.service.profile, self.vault, skipped=[])
        self.assertNotIn(path, files)

    def test_no_write_without_save(self):
        before = snapshot(self.vault)
        self.correct()
        self.assertEqual(snapshot(self.vault), before)

    def test_refusals_hold_nothing_and_write_nothing(self):
        before = snapshot(self.vault)
        cases = ((dict(aid="b" * 32), "unknown"), (dict(source="3"), "source"),
                 (dict(source="2"), "changed"),                     # unlabelled: not served
                 (dict(text="the client moved it"), "engagement"),
                 (dict(domain="personal"), "personal"), (dict(domain="maybe"), "domain"),
                 (dict(text="my password is hunter2"), "secret"))
        for kw, reason in cases:
            code, body = self.correct(**kw)
            self.assertEqual((code, body["status"]), (200, "refused"), kw)
            msg = capture.REFUSALS.get(reason) or capture.CORRECT_REFUSALS[reason]
            self.assertEqual((body["display"], body["spoken"]), (msg, msg), kw)
            self.assertNotIn("id", body)
        self.assertEqual(len(self.service.drafts), 0)
        self.assertEqual(snapshot(self.vault), before)

    def test_a_target_outside_the_allowlist_or_missing_is_refused(self):
        for path in (".obsidian/workspace.md", "unlabelled.md", "10 Projects/missing.md",
                     "../outside.md"):
            self.service.cited.put(self.aid, [(path, "0" * 64)])
            _, body = self.correct()
            self.assertEqual(body["status"], "refused", path)
            self.assertIn(body["display"], (capture.CORRECT_REFUSALS["changed"],
                                            capture.CORRECT_REFUSALS["target"]))
        self.assertEqual(len(self.service.drafts), 0)

    def test_a_target_whose_words_are_refused_is_refused(self):
        p = self.vault / "Work"
        p.mkdir()
        (p / "client-workshop.md").write_text("---\nclassification: internal\n---\nx\n")
        self.service.cited.put(self.aid, [("Work/client-workshop.md",
                                           sha(p / "client-workshop.md"))])
        _, body = self.correct()
        self.assertEqual(body["display"], capture.REFUSALS["engagement"])

    def test_a_target_changed_since_the_answer_is_refused_at_draft_and_at_save(self):
        _, body = self.correct()
        (self.vault / TARGET).write_text((self.vault / TARGET).read_text() + "\nEdited.\n")
        before = snapshot(self.vault)
        _, saved = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(saved["status"], "changed")
        self.assertEqual(snapshot(self.vault), before)
        _, again = self.correct()
        self.assertEqual(again["display"], capture.CORRECT_REFUSALS["changed"])

    def test_an_already_corrected_target_cannot_be_corrected_again(self):
        _, body = self.correct()
        self.request("POST", "/capture/save", {"id": body["id"]})
        _, again = self.correct()
        self.assertEqual(again["display"], capture.CORRECT_REFUSALS["changed"])

    def test_save_is_single_use(self):
        _, body = self.correct()
        self.request("POST", "/capture/save", {"id": body["id"]})
        before = snapshot(self.vault)
        _, again = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(again["status"], "repeat")
        self.assertEqual(snapshot(self.vault), before)

    def test_model_output_that_fails_the_guards_falls_back_to_his_words(self):
        for reply in ("Call the client on Friday.", "", "x" * 700, "Password is abc123def456"):
            self.model.reply = reply
            _, body = self.correct(text="Tempo runs are on Fridays now")
            self.assertEqual(body["status"], "draft", reply)
            self.assertIn(": Tempo runs are on Fridays now. The note", body["spoken"])

    def test_a_failing_model_falls_back_to_his_words(self):
        def boom():
            raise RuntimeError("down")
        self.service.model_factory = boom
        _, body = self.correct(text="Tempo runs are on Fridays now")
        self.assertIn(": Tempo runs are on Fridays now. The note", body["spoken"])
        self.assertFalse(self.service.busy.locked())

    def test_a_busy_model_is_skipped(self):
        self.service.busy.acquire()
        try:
            _, body = self.correct(text="Tempo runs are on Fridays now")
        finally:
            self.service.busy.release()
        self.assertIn(": Tempo runs are on Fridays now. The note", body["spoken"])
        self.assertEqual(self.model.calls, [])

    def test_a_slow_model_is_skipped_and_keeps_the_lock_until_it_ends(self):
        gate = threading.Event()

        class Slow:
            def generate(self, prompt, max_tokens=320):
                gate.wait(5)
                return "Late text.", 0.0
        self.service.model_factory = Slow
        old = serve.CORRECT_MODEL_TIMEOUT
        serve.CORRECT_MODEL_TIMEOUT = 0.2
        try:
            _, body = self.correct(text="Tempo runs are on Fridays now")
            self.assertIn(": Tempo runs are on Fridays now. The note", body["spoken"])
            self.assertTrue(self.service.busy.locked(), "the model call is still running")
        finally:
            gate.set()
            serve.CORRECT_MODEL_TIMEOUT = old
        for _ in range(50):
            if not self.service.busy.locked():
                break
            threading.Event().wait(0.05)
        self.assertFalse(self.service.busy.locked())

    def test_a_plain_capture_draft_can_never_hide_a_note(self):
        """A capture's bytes that somehow named a target are refused at save."""
        name, data = capture.render_correction("Tempo is fast", "work", TARGET, NOW)
        draft_id = self.service.drafts.add("Tempo is fast", "work", name, data)  # no target
        before = snapshot(self.vault)
        _, saved = self.request("POST", "/capture/save", {"id": draft_id})
        self.assertEqual(saved["status"], "error")
        self.assertEqual(snapshot(self.vault), before)

    def test_the_answer_records_paths_and_hashes_only(self):
        self.model.reply = "Tempo runs happen on Thursdays [E1]"
        _, ans = self.request("POST", "/ask", {"question": "When do tempo runs happen?"})
        cited = self.service.cited.get(ans["answer_id"])
        self.assertEqual([p for p, _ in cited], [c["path"] for c in ans["citations"]])
        for p, h in cited:
            self.assertEqual(h, sha(self.vault / p))

    def test_cited_notes_expire_with_the_wrong_window(self):
        clock = [1000.0]
        t = serve.CitedNotes(mono=lambda: clock[0])
        t.put("x", [(TARGET, "h")])
        clock[0] += serve.RECENT_WINDOW
        self.assertIsNone(t.get("x"))
        self.assertEqual(len(t.items), 0)

    def test_idle_cited_notes_are_swept(self):
        clock = [1000.0]
        t = serve.CitedNotes(mono=lambda: clock[0])
        t.put("x", [(TARGET, "h")])
        clock[0] += serve.RECENT_WINDOW
        t.sweep()
        self.assertEqual(len(t.items), 0)
        self.assertIn("service.cited.start_sweeper()", open(serve.__file__).read())

    def test_bad_bodies(self):
        good = {"answer_id": self.aid, "source": "1", "text": "x", "domain": "work"}
        for change in ({"source": "0"}, {"source": "10"}, {"source": True}, {"source": 1.5},
                       {"answer_id": "short"}, {"text": 3}, {"domain": "w" * 17},
                       {"extra": 1}):
            body = dict(good, **change)
            code, out = self.request("POST", "/correct", body)
            self.assertEqual(code, 400, change)
            self.assertEqual(out["display"], serve.CAPTURE_BAD_REQUEST)
        code, out = self.request("POST", "/correct", raw=b"not json")
        self.assertEqual(code, 400)
        code, out = self.request("POST", "/correct", dict(good, source=1))
        self.assertEqual((code, out["status"]), (200, "draft"))

    def test_the_route_needs_the_token(self):
        code, _ = self.request("POST", "/correct", {"answer_id": self.aid, "source": "1",
                                                    "text": "x", "domain": "work"}, token=None)
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
