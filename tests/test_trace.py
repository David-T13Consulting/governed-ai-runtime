import json
import os
import tempfile
import unittest
from pathlib import Path

from dbrain import adapter, answer, config, trace
from tests import fake_vault


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.vault, _, _ = fake_vault.build()
        self.profile = fake_vault.PROFILE
        self.artifacts = Path(tempfile.mkdtemp(prefix="dbrain-traces-"))

    def run_one(self, question="What does tempo mean?"):
        model = adapter.StubAdapter(reply="Tempo means marathon pace or faster [E1]")
        result = answer.ask(question, profile=self.profile, model=model, vault=self.vault)
        path = trace.write(result, model, self.profile, artifacts=self.artifacts)
        return result, json.loads(Path(path).read_text().splitlines()[-1]), path

    def test_trace_is_written_outside_the_vault(self):
        _, _, path = self.run_one()
        self.assertFalse(str(path).startswith(str(self.vault)))

    def test_trace_hashes_the_question_and_never_stores_it(self):
        question = "What does tempo mean?"
        _, record, path = self.run_one(question)
        self.assertEqual(len(record["question_sha256"]), 64)
        self.assertNotIn(question, Path(path).read_text())
        self.assertNotIn("tempo", Path(path).read_text().lower().replace("tempo_block", ""))

    def test_trace_carries_the_answer_id_it_is_given_and_none_otherwise(self):
        # P2.7 step C5: the random id the phone got, so a "wrong" tap ties to this line.
        _, record, _ = self.run_one()
        self.assertIsNone(record["answer_id"])
        model = adapter.StubAdapter(reply="Tempo means marathon pace or faster [E1]")
        result = answer.ask("What does tempo mean?", profile=self.profile, model=model,
                            vault=self.vault)
        path = trace.write(result, model, self.profile, artifacts=self.artifacts,
                           answer_id="a" * 32)
        self.assertEqual(json.loads(Path(path).read_text().splitlines()[-1])["answer_id"], "a" * 32)

    def test_trace_records_paths_timings_and_model_version(self):
        _, record, _ = self.run_one()
        self.assertTrue(record["paths_retrieved"])
        for p in record["paths_retrieved"]:
            self.assertTrue(p.endswith(".md"))
        # Indexing reads every allowed note, so the trace must list those too, or the
        # audit trail understates what the query opened.
        self.assertTrue(record["files_indexed"])
        self.assertGreaterEqual(record["files_indexed_count"], len(record["paths_retrieved"]))
        self.assertIn("timing", record)
        self.assertIn("model", record)
        self.assertIn("adapter", record["model"])
        self.assertEqual(record["profile"], self.profile.name)
        self.assertIn(record["status"], ("supported", "incomplete", "withheld"))

    def test_trace_notes_cloud_keys_without_using_them(self):
        os.environ["OPENAI_API_KEY"] = "sk-not-real"
        try:
            _, record, _ = self.run_one()
            self.assertIn("OPENAI_API_KEY", record["cloud_keys_present_but_unused"])
        finally:
            os.environ.pop("OPENAI_API_KEY", None)

    def test_a_failed_query_is_still_traced(self):
        """Round D finding 3: a malformed model reply read the corpus and called the
        model, so the CLI must leave a trace before it fails."""
        from dbrain import cli

        class Broken(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                raise adapter.MalformedResponse("generation did not finish")

        saved = (cli.adapter_mod.StubAdapter, config.LOGS, config.PROFILES.get("default"))
        cli.adapter_mod.StubAdapter = Broken
        config.LOGS = self.artifacts
        config.PROFILES["default"] = self.profile
        try:
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                rc = cli.main(["What does tempo mean?", "--stub-model"])
        finally:
            cli.adapter_mod.StubAdapter, config.LOGS, config.PROFILES["default"] = saved
        self.assertEqual(rc, 3)
        traces = list(self.artifacts.glob("traces-*.jsonl"))
        self.assertEqual(len(traces), 1)
        record = json.loads(traces[0].read_text().splitlines()[-1])
        self.assertEqual(record["status"], answer.FAILED)
        self.assertIn("MalformedResponse", record["reason"])
        self.assertTrue(record["files_indexed"])
        self.assertNotIn("tempo", traces[0].read_text().lower().replace("tempo_block", ""))


    def test_remote_error_text_never_reaches_the_trace(self):
        """Round E finding 4: a model error that echoes the question stays out of traces."""
        from dbrain import cli
        secret_q = "What does tempo mean ZZQUESTIONMARKERZZ?"

        class Echoing(adapter.StubAdapter):
            def generate(self, prompt, max_tokens=320):
                raise adapter.MalformedResponse(
                    "local model returned an error: {'message': %r, 'url': 'http://x/?q=%s'}"
                    % (secret_q, secret_q))

        saved = (cli.adapter_mod.StubAdapter, config.LOGS, config.PROFILES.get("default"))
        cli.adapter_mod.StubAdapter = Echoing
        config.LOGS = self.artifacts
        config.PROFILES["default"] = self.profile
        try:
            import io, contextlib
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main([secret_q, "--stub-model"])
        finally:
            cli.adapter_mod.StubAdapter, config.LOGS, config.PROFILES["default"] = saved
        text = "".join(p.read_text() for p in self.artifacts.glob("traces-*.jsonl"))
        self.assertTrue(text)
        self.assertNotIn("ZZQUESTIONMARKERZZ", text)
        self.assertIn('"reason": "MalformedResponse"', text)

if __name__ == "__main__":
    unittest.main()
