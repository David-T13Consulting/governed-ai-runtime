"""PL1 step 6, "remember from the phone": the guards, the draft and the two routes.

Every test that writes uses a synthetic temporary vault; nothing here touches the real one.
"""
import datetime
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from dbrain import capture, index, serve
from tests.test_serve import TOKEN, EndpointFixture

NOW = datetime.datetime(2026, 10, 22, 14, 5)


def refused(text, domain="work"):
    return capture.check(text, domain)[0]


class AdmissionTest(unittest.TestCase):
    def test_a_plain_work_note_is_admitted(self):
        reason, ok = capture.check("  Scaleway bills  per token;\n ask about EU regions  ", "Work")
        self.assertIsNone(reason)
        self.assertEqual(ok, ("Scaleway bills per token; ask about EU regions", "work"))

    def test_personal_is_refused_until_the_wave1_gate(self):
        """Q153 (b), Q156: personal captures wait for P1.4 and the lint check."""
        self.assertFalse(capture.WAVE1_GATE_MET)
        self.assertEqual(refused("Buy oat milk", "personal"), "personal")
        self.assertEqual(refused("Buy oat milk", "Personal"), "personal")

    def test_no_clear_domain_saves_nothing(self):
        """AGENTS.md rule 6: unclear classification stops."""
        for d in ("", "both", "work or personal", "travail", "professional", None, 3, "work\n"
                  "personal"):
            self.assertEqual(refused("Read the Mistral paper", d), "domain", d)

    def test_form(self):
        self.assertEqual(refused(""), "empty")
        self.assertEqual(refused("   \n "), "empty")
        self.assertEqual(refused("word " * 121), "too_long")
        self.assertIsNone(refused(("word " * 120)[:capture.MAX_CAPTURE]))
        for bad in ("a\x00b", "a‮b", "a​b", "ab", "a\x1bb", None, 5):
            self.assertEqual(refused(bad), "unreadable", repr(bad))


class GuardTest(unittest.TestCase):
    """The second line (Q154 (a)): it refuses what it detects, EN and FR."""

    CASES = {
        "secret": [
            "My password is hunter2", "Le mot de passe du wifi est soleil", "mdp: soleil",
            # Key shapes are assembled at run time so the repository's secret scan,
            # which reads this file, never sees a key-shaped literal.
            "PIN code 4321", "the key is " + "sk" + "-abc123def456ghi",
            "gh" + "p_" + "ABCDEFGHIJKLMNOP1234", "token " + "xo" + "xb-1234-abcd",
            "AK" + "IA" + "IOSFODNN7EXAMPLE", "ey" + "JhbGciOiJIUzI1NiJ9 header",
            "Q3vT9pLm" + "2XkR8sWz is it", "-----" + "BEGIN OPENSSH PRIVATE " + "KEY-----",
            "recovery codes for GitHub", "the token is abcdef",
        ],
        "id_number": [
            "Card 4111 1111 1111 1111", "IBAN BE68 5390 0754 7034", "BE68539007547034",
            "My national number is 85.07.30-033.61", "Passport EM1234567",
            "four one one one one one one one one", "numéro de compte à vérifier",
            "the social security office",
        ],
        "engagement": [
            "Call Marc about the client workshop", "Le client veut un atelier", "Engagement review on Friday",
            "Send the SOW draft", "NDA to sign", "Deliverables due Monday", "Livrable pour jeudi",
            "Invoice the Q3 work", "Proposal for Acme next week", "Nouvelle mission en janvier",
            "Customer feedback on the pilot", "Acme follow-up",
        ],
        "child": [
            "Take my son to climbing", "Mon fils a piscine", "Child A has scouts",
            "School closed Monday", "L'école ferme lundi", "Pick up the kids", "La crèche ferme",
            "Daughter's homework", "babysitter on Friday",
        ],
        "health": [
            "Appointment with the specialist on Tuesday", "Rendez-vous chez le médecin jeudi",
            "Dentist at 9", "New medication from the pharmacy", "Kiné à 18h", "Back pain again",
            "MRI results", "therapy session", "Ma santé d'abord",
        ],
        "finance": [
            "Salary review in March", "Mon salaire augmente", "Mortgage rate fixed",
            "Pay the tax return", "Impôts à payer", "Bank account at ING", "Ma banque a appelé",
            "Pension contribution", "My investments are down",
        ],
        "journal": ["Dear diary, today was long", "Mon journal intime"],
    }

    def test_each_class_refuses_its_cases(self):
        for cls, texts in self.CASES.items():
            for t in texts:
                self.assertEqual(refused(t), cls, t)

    def test_refusal_text_never_quotes_the_capture(self):
        for cls, texts in self.CASES.items():
            for t in texts:
                reason = refused(t + " Zorblax")
                self.assertEqual(reason, cls, t)
                self.assertNotIn("zorblax", capture.REFUSALS[reason].lower())

    def test_ordinary_work_notes_pass(self):
        """The lists must not swallow David's ordinary work vocabulary (EN and FR)."""
        for t in ("Learned that Scaleway bills Gemma 4 26B-A4B per token",
                  "Note that llama.cpp b11146 is pinned", "son projet avance bien",
                  "acheter du pain pour la réunion", "Q153 answered, P1.4 starts now",
                  "Read the API key rotation guide", "Idea: a router that escalates once",
                  "Le prochain atelier produit est jeudi à 14h", "Rates for 2027: review in December",
                  "Qwen3-235B looks strong on French", "Meeting moved to 22/10/2026",
                  "Un niveau élevé de bruit dans les traces", "Try Swift for the iPhone app",
                  "colleague recommended the Product School course", "Health check endpoint returns ok"):
            reason = refused(t)
            if t == "Health check endpoint returns ok":
                self.assertEqual(reason, "health")   # fails closed; documented false positive
                continue
            self.assertIsNone(reason, t)

    def test_accents_case_and_punctuation_do_not_hide_a_term(self):
        for t in ("CLIENT meeting", "Clients' list", "client-side workshop", "Médecin", "ÉCOLE",
                  "e-cole".replace("-", ""), "mot-de-passe"):
            self.assertIsNotNone(refused(t), t)

    def test_allowed_phrases_are_exact(self):
        self.assertIsNone(refused("Product School course on agents"))
        self.assertEqual(refused("Product School course for my son's school"), "child")
        self.assertEqual(refused("product schools"), "child")

    def test_the_order_of_checks_is_fixed(self):
        self.assertEqual(refused("client password hunter2"), "secret")
        self.assertEqual(refused("client IBAN BE68 5390 0754 7034"), "id_number")
        self.assertEqual(refused("my son and the client"), "engagement")


class RenderTest(unittest.TestCase):
    def test_exact_bytes(self):
        name, data = capture.render("Scaleway bills per token", "work", NOW)
        self.assertEqual(name, "2026-10-22-1405-scaleway-bills-per-token.md")
        self.assertEqual(data.decode("utf-8"), "\n".join([
            "---", "classification: internal", "domain: professional", "sensitivity: ordinary",
            "processing: approved-external", "sharing: private", "source: voice-capture",
            "note_type: capture", "review_status: unreviewed", "created: 2026-10-22",
            "updated: 2026-10-22", "---", "# Phone capture, Thu 22 Oct 2026, 14:05", "",
            "Scaleway bills per token", ""]))
        self.assertEqual(index.classification_of(data.decode()), "internal")
        self.assertTrue(capture.NAME.fullmatch(name))

    def test_frontmatter_cannot_be_reached_from_the_text(self):
        """The text is one line after the heading; a fence or a label in it stays body."""
        for t in ("--- classification: client ---", "classification: client",
                  "---\nclassification: client\n---"):
            name, data = capture.render(t, "work", NOW)
            text = data.decode()
            self.assertEqual(index.classification_of(text), "internal", t)
            self.assertEqual(text.count("\n---\n"), 1)

    def test_slug_is_ascii_and_bounded(self):
        self.assertEqual(capture.slug("Réunion à l'équipe produit, jeudi!"),
                         "reunion-a-l-equipe-produit-jeudi")
        self.assertLessEqual(len(capture.slug("word " * 50)), 48)
        self.assertEqual(capture.slug("!!!"), "capture")
        name, _ = capture.render("日本語のメモ", "work", NOW)
        self.assertEqual(name, "2026-10-22-1405-capture.md")

    def test_render_refuses_an_unadmitted_domain(self):
        with self.assertRaises(ValueError):
            capture.render("text", "maybe", NOW)

    def test_read_back_repeats_the_words_and_the_label(self):
        said = capture.read_back("  Scaleway   bills per token ", "work")
        self.assertEqual(said, "Work note for your Inbox: Scaleway bills per token. "
                               "Say Save to keep it.")


class CaptureRouteTest(EndpointFixture):
    def setUp(self):
        super().setUp()
        self.service.now = lambda: NOW
        self.inbox = self.vault / "00 Inbox"

    def inbox_files(self):
        return sorted(p.name for p in self.inbox.iterdir())

    def test_draft_then_save_writes_exactly_one_new_inbox_note(self):
        before = self.inbox_files()
        code, body = self.request("POST", "/capture", {"text": "Scaleway bills per token",
                                                       "domain": "Work"})
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["spoken"], "Work note for your Inbox: Scaleway bills per token. "
                                         "Say Save to keep it.")
        self.assertIn("00 Inbox/2026-10-22-1405-scaleway-bills-per-token.md", body["display"])
        self.assertEqual(self.inbox_files(), before, "a draft must not write")
        code, saved = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((code, saved["status"]), (200, "saved"))
        self.assertEqual(saved["path"], "00 Inbox/2026-10-22-1405-scaleway-bills-per-token.md")
        self.assertEqual(saved["spoken"], "Saved to your Inbox.")
        self.assertEqual(self.inbox_files(), sorted(before + [
            "2026-10-22-1405-scaleway-bills-per-token.md"]))
        _, data = capture.render("Scaleway bills per token", "work", NOW)
        self.assertEqual((self.inbox / "2026-10-22-1405-scaleway-bills-per-token.md").read_bytes(),
                         data)

    def test_a_saved_note_is_found_by_the_next_question(self):
        _, body = self.request("POST", "/capture", {"text": "Zebrafish workshop moved to Thursday",
                                                    "domain": "work"})
        self.request("POST", "/capture/save", {"id": body["id"]})
        chunks, files = index.build(self.service.profile, self.vault, skipped=[])
        self.assertIn("00 Inbox/2026-10-22-1405-zebrafish-workshop-moved-to-thursday.md", files)

    def test_a_save_is_single_use(self):
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        self.request("POST", "/capture/save", {"id": body["id"]})
        n = len(self.inbox_files())
        code, again = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((code, again["status"], again["display"]),
                         (200, "repeat", serve.SAVE_REPLIES["repeat"]))
        self.assertEqual(len(self.inbox_files()), n)

    def test_an_expired_draft_is_never_written(self):
        clock = [1000.0]
        self.service.drafts.mono = lambda: clock[0]
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        clock[0] += serve.DRAFT_TTL
        before = self.inbox_files()
        _, out = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(self.inbox_files(), before)
        self.assertEqual(len(self.service.drafts), 0, "expired drafts are dropped")

    def test_two_notes_in_one_minute_get_distinct_names(self):
        for _ in range(2):
            _, body = self.request("POST", "/capture", {"text": "Same words", "domain": "work"})
            self.request("POST", "/capture/save", {"id": body["id"]})
        names = [n for n in self.inbox_files() if "same-words" in n]
        self.assertEqual(names, ["2026-10-22-1405-same-words-2.md", "2026-10-22-1405-same-words.md"])

    def test_refusals_hold_nothing_and_write_nothing(self):
        before = self.inbox_files()
        for text, domain, reason in (("Call about the client workshop", "work", "engagement"),
                                     ("Buy oat milk", "personal", "personal"),
                                     ("Read it", "maybe", "domain"),
                                     ("My password is hunter2", "work", "secret")):
            code, body = self.request("POST", "/capture", {"text": text, "domain": domain})
            self.assertEqual((code, body["status"]), (200, "refused"))
            self.assertEqual(body["display"], capture.REFUSALS[reason])
            self.assertEqual(body["spoken"], body["display"])
            self.assertNotIn("id", body)
        self.assertEqual(len(self.service.drafts), 0)
        self.assertEqual(self.inbox_files(), before)
        code, body = self.request("POST", "/capture/save", {"id": ""})
        self.assertEqual((code, body["status"]), (200, "none"))

    def test_bad_bodies(self):
        for raw in (b"not json", json.dumps({"text": "x"}).encode(),
                    json.dumps({"text": "x", "domain": "work", "extra": 1}).encode(),
                    json.dumps({"text": 1, "domain": "work"}).encode(),
                    json.dumps({"text": "x", "domain": "w" * 17}).encode(),
                    json.dumps(["x", "work"]).encode()):
            code, body = self.request("POST", "/capture", raw=raw)
            self.assertEqual(code, 400, raw)
            self.assertEqual(body["display"], serve.CAPTURE_BAD_REQUEST)
            self.assertEqual(body["spoken"], body["display"])
        code, body = self.request("POST", "/capture", raw=b"x" * (serve.MAX_BODY + 1))
        self.assertEqual((code, body["display"]), (413, serve.CAPTURE_BAD_REQUEST))
        for raw in (json.dumps({"id": "z" * 32}).encode(), json.dumps({"id": "a" * 32, "x": 1}).encode()):
            code, body = self.request("POST", "/capture/save", raw=raw)
            self.assertEqual((code, body["status"]), (200, "none"))

    def test_auth_and_peer_rules_apply(self):
        code, _ = self.request("POST", "/capture", {"text": "x", "domain": "work"}, token="w" * 40)
        self.assertEqual(code, 401)
        code, _ = self.request("POST", "/capture/save", {"id": "a" * 32}, token=None)
        self.assertEqual(code, 401)
        self.assertEqual(len(self.service.drafts), 0)

    def test_capture_never_reaches_the_model_the_store_or_the_log(self):
        calls = []
        self.service.model_factory = lambda: calls.append(1)
        self.service.failures = mock.Mock()
        err = io.StringIO()
        with mock.patch("sys.stderr", err), \
                mock.patch.object(serve.trace, "write") as traced:
            _, body = self.request("POST", "/capture", {"text": "Zanzibar roadmap idea",
                                                        "domain": "work"})
            self.request("POST", "/capture/save", {"id": body["id"]})
            self.request("POST", "/capture", {"text": "Zanzibar client deal", "domain": "work"})
            _, body = self.request("POST", "/capture", {"text": "Zanzibar plan", "domain": "work"})
            with mock.patch.object(serve.writer, "create_inbox_note",
                                   side_effect=serve.writer.WriteRefused("x")):
                self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(traced.mock_calls, [], "a capture reached the trace writer")
        self.assertEqual(calls, [])
        self.assertEqual(self.service.failures.mock_calls, [])
        log = err.getvalue()
        self.assertNotIn("Zanzibar", log)
        self.assertIn("POST /capture capture-draft", log)
        self.assertIn("POST /capture/save save-saved", log)
        self.assertIn("POST /capture capture-refused-engagement", log)

    def test_save_rechecks_the_draft_at_save_time(self):
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        before = self.inbox_files()
        with mock.patch.object(capture, "check", return_value=("personal", None)):
            _, out = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(out["status"], "error")
        self.assertEqual(self.inbox_files(), before)
        _, again = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(again["status"], "unknown", "a failed save does not leave a draft behind")

    def test_a_write_refusal_reports_not_saved(self):
        os.rename(self.inbox, self.vault / "Inbox-moved")
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        _, out = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((out["status"], out["display"]), ("error", serve.SAVE_REPLIES["error"]))
        self.assertFalse(self.inbox.exists(), "the writer creates no folder")

    def test_a_save_placed_but_not_confirmed_is_never_repeated(self):
        """Review 1/2 blocker 2: "check", never "say it again", and the approval is used."""
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        with mock.patch.object(serve.writer, "create_inbox_note",
                               side_effect=serve.writer.NoteChanged("x")):
            _, out = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((out["status"], out["display"]), ("check", serve.SAVE_REPLIES["check"]))
        self.assertNotIn("Say it again", out["display"])
        _, again = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual(again["status"], "repeat")

    def test_a_temporary_file_left_is_said(self):
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        with mock.patch.object(serve.writer, "create_inbox_note",
                               side_effect=serve.writer.TempLeft("x")):
            _, out = self.request("POST", "/capture/save", {"id": body["id"]})
        self.assertEqual((out["status"], out["display"]), ("left", serve.SAVE_REPLIES["left"]))

    def test_concurrent_saves_write_once(self):
        import threading
        _, body = self.request("POST", "/capture", {"text": "Read the Mistral paper", "domain": "work"})
        out = []
        ts = [threading.Thread(target=lambda: out.append(
            self.request("POST", "/capture/save", {"id": body["id"]})[1]["status"])) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(out.count("saved"), 1, out)
        self.assertTrue(set(out) <= {"saved", "repeat", "busy"}, out)
        self.assertEqual(len([n for n in self.inbox_files() if "mistral" in n]), 1)


class DraftTableTest(unittest.TestCase):
    def test_capacity_drops_the_oldest_and_its_bytes(self):
        t = serve.CaptureDrafts(capacity=2)
        a = t.add("a", "work", "n", b"x")
        held = t.items[a]
        t.add("b", "work", "n", b"x")
        t.add("c", "work", "n", b"x")
        self.assertNotIn(a, t.items)
        self.assertIsNone(held.data)
        self.assertEqual(t.claim(a), "unknown")

    def test_idle_drafts_expire_without_any_request(self):
        """Review 1/2 should-fix: the sweeper drops a draft nobody asks about again."""
        clock = [0.0]
        t = serve.CaptureDrafts(mono=lambda: clock[0])
        i = t.add("words", "work", "n", b"bytes")
        held = t.items[i]
        clock[0] = serve.DRAFT_TTL - 1
        t.sweep()
        self.assertEqual(len(t), 1)
        clock[0] = serve.DRAFT_TTL
        t.sweep()
        self.assertEqual(len(t), 0)
        self.assertIsNone(held.data)

    def test_the_service_starts_the_draft_sweeper(self):
        src = Path(serve.__file__).read_text()
        self.assertIn("service.drafts.start_sweeper()", src)

    def test_after_a_save_only_the_outcome_is_kept(self):
        t = serve.CaptureDrafts()
        i = t.add("words", "work", "n", b"bytes")
        d = t.claim(i)
        t.finish(i, d, True)
        self.assertIsNone(t.items[i].data)
        self.assertIsNone(t.items[i].text)
        self.assertEqual(t.claim(i), "repeat")


if __name__ == "__main__":
    unittest.main()
