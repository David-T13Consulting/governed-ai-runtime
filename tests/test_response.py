import unittest

from dbrain import adapter, config


def ok(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class ResponseParsingTest(unittest.TestCase):
    """Round C finding 3: anything that is not a complete text answer fails loudly."""

    def test_a_complete_answer_is_returned(self):
        self.assertEqual(adapter.chat_content(ok("Tempo is [E1].")), "Tempo is [E1].")

    def test_an_error_object_is_not_an_empty_answer(self):
        with self.assertRaises(adapter.MalformedResponse):
            adapter.chat_content({"error": {"code": 500, "message": "boom"}})

    def test_missing_choices_fail(self):
        for payload in ({}, {"choices": []}, {"choices": [{}]}, [], "text"):
            with self.assertRaises(adapter.MalformedResponse, msg=repr(payload)):
                adapter.chat_content(payload)

    def test_non_string_content_fails_without_a_type_error(self):
        for content in (None, 42, ["a"], {"text": "a"}):
            with self.assertRaises(adapter.MalformedResponse, msg=repr(content)):
                adapter.chat_content(ok(content))

    def test_a_truncated_generation_fails(self):
        for finish in ("length", None, "abort"):
            with self.assertRaises(adapter.MalformedResponse, msg=repr(finish)):
                adapter.chat_content(ok("half an ans", finish=finish))

    def test_an_unterminated_think_block_fails(self):
        with self.assertRaises(adapter.MalformedResponse):
            adapter.strip_think("<think>still reasoning about [E1]")

    def test_a_closed_think_block_is_dropped(self):
        self.assertEqual(adapter.strip_think("<think>\n</think>\nAnswer [E1]"), "Answer [E1]")

    def test_malformed_is_a_loud_model_failure(self):
        self.assertTrue(issubclass(adapter.MalformedResponse, adapter.ModelUnavailable))


class RequestSignatureTest(unittest.TestCase):
    """Round C finding 4: the version must change when the request shape changes."""

    def test_system_turn_and_params_are_in_the_signature(self):
        base = adapter.request_signature("Q: %s E: %s")
        saved = adapter.SYSTEM
        try:
            adapter.SYSTEM = saved + " changed"
            self.assertNotEqual(base, adapter.request_signature("Q: %s E: %s"))
        finally:
            adapter.SYSTEM = saved
        self.assertNotEqual(base, adapter.request_signature("Q: %s E: %s", max_tokens=64))
        self.assertEqual(base, adapter.request_signature("Q: %s E: %s"))


class DevcasesCorpusTest(unittest.TestCase):
    """Round C finding 1: exactly the three files the brief names, and no root."""

    def test_both_profiles_are_the_three_named_files(self):
        for p in (config.DEFAULT, config.DEVCASES):
            self.assertEqual(p.roots, (), p.name)
            self.assertEqual(set(p.files), {
                "AGENTS.md",
                "10 Projects/AI Second Brain/02_Architecture/data-policy.md",
                "10 Projects/AI Second Brain/04_Roadmap.md",
            }, p.name)
            self.assertIn("10 Projects/AI Second Brain/04_Roadmap.md", p.sections)

    def test_the_frozen_corpus_is_byte_for_byte_what_c2_read(self):
        """The dev cases read a frozen corpus, not the live vault. Here: the sample vault."""
        import hashlib
        import run_dev_cases
        from dbrain import index
        record = {"version": "9ab30d4a7a82", "chunk_count": 10, "files": {
            "AGENTS.md":
                "5d1e5ce20926c995f6d10f45a93262138d2b74cfea6fb1925bf89843ae3f82ff",
            "10 Projects/AI Second Brain/02_Architecture/data-policy.md":
                "645d3b142a6293d302382d04bbf71f4022158c1a3d479bd0a4a61c1b6f25f05c",
            "10 Projects/AI Second Brain/04_Roadmap.md":
                "8cba4c2d8ca621442ad0c4460c7e0a4e8e69973dc7d0a20dcfe6f061b6571a99"}}
        for p in (config.DEFAULT, config.DEVCASES):
            self.assertEqual(p.vault, config.DEVCASE_CORPUS, p.name)
            chunks, files = index.build(p)
            self.assertEqual(run_dev_cases.corpus_version(chunks), record["version"], p.name)
            self.assertEqual(len(chunks), record["chunk_count"], p.name)
        for rel, digest in record["files"].items():
            data = (config.DEVCASE_CORPUS / rel).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest, rel)
        # The phone profile still reads the live vault.
        self.assertIsNone(config.MVP_WHOLE_VAULT.vault)


    def test_an_empty_or_thinking_only_answer_fails(self):
        """Round D finding 4: no answer text is not a bad answer, it is no answer."""
        for content in ("", "   \n", "<think>\n</think>", "<think>reasoning</think>\n  "):
            with self.assertRaises(adapter.MalformedResponse, msg=repr(content)):
                adapter.answer_text(ok(content))

    def test_answer_text_keeps_a_real_answer(self):
        self.assertEqual(adapter.answer_text(ok("<think></think>\nTempo [E1]")), "Tempo [E1]")



class NonUtf8ReplyTest(unittest.TestCase):
    """Round F finding 3: undecodable bytes are a malformed reply, traced like others."""

    def test_non_utf8_bytes_become_malformed_and_carry_a_partial_result(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from dbrain import answer
        from tests import fake_vault

        class Bad(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b"\xff\xfe not utf-8"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), Bad)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            model = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % srv.server_address[1], timeout=5)
            vault, _, _ = fake_vault.build()
            with self.assertRaises(adapter.MalformedResponse) as ctx:
                answer.ask("What does tempo mean?", profile=fake_vault.PROFILE, model=model, vault=vault)
            self.assertEqual(ctx.exception.partial.status, answer.FAILED)
            self.assertEqual(ctx.exception.partial.reason, "MalformedResponse")
        finally:
            srv.shutdown()

class IncompleteReadTest(unittest.TestCase):
    """Round G: a truncated HTTP response is a model failure, and the CLI still traces it."""

    def test_incomplete_read_is_traced_through_the_cli(self):
        import contextlib, io, json, socket, tempfile, threading
        from pathlib import Path
        from dbrain import cli, config
        from tests import fake_vault

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def serve_short():
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"choices\":")
            conn.close()

        threading.Thread(target=serve_short, daemon=True).start()
        endpoint = "http://127.0.0.1:%d" % listener.getsockname()[1]
        artifacts = Path(tempfile.mkdtemp(prefix="dbrain-ir-"))
        vault, _, _ = fake_vault.build()
        saved = (config.LOGS, config.PROFILES.get("default"), config.VAULT)
        config.LOGS, config.PROFILES["default"], config.VAULT = artifacts, fake_vault.PROFILE, vault
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(["What does tempo mean?", "--endpoint", endpoint])
        finally:
            config.LOGS, config.PROFILES["default"], config.VAULT = saved
            listener.close()
        self.assertEqual(rc, 3)
        traces = list(artifacts.glob("traces-*.jsonl"))
        self.assertEqual(len(traces), 1)
        record = json.loads(traces[0].read_text().splitlines()[-1])
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["reason"], "ModelUnavailable")
        self.assertNotIn("tempo", traces[0].read_text().lower().replace("tempo_block", ""))

if __name__ == "__main__":
    unittest.main()
