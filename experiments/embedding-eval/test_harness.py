"""M6b S3: tests of the harness on a tiny made-up corpus only. The real cases are never
scored here (that is S4).

    python3 -m unittest discover -s experiments/embedding-eval -p "test_*.py"

Runs under the system Python and the M6b venv; nothing here imports torch.
"""
import hashlib
import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402

TOY_CHUNKS = [
    {"id": "C1", "heading": "# Kettles", "text": "A kettle boils water for tea in the kitchen."},
    {"id": "C2", "heading": "## Bicycles", "text": "Pump the bicycle tyres before a long ride."},
    {"id": "C3", "heading": "## Gardens", "text": "Water the tomato plants in the garden at dusk."},
    {"id": "C4", "heading": "## Kettles again", "text": "Descale the kettle when limescale builds up."},
    {"id": "C5", "heading": "## Lamps", "text": "Replace the lamp bulb with a warm white one."},
    {"id": "C6", "heading": "## Ropes", "text": "Coil the rope loosely so it does not tangle."},
]
TOY_CASES = [
    {"id": "tea", "question": "How do I boil water for tea?"},
    {"id": "tyres", "question": "What to do with tyres before riding?"},
    {"id": "gap", "question": "Where are the spare keys kept?"},
    {"id": "nothing", "question": "Zorblium quantelflax?"},
]
TOY_LABELS = {"tea": {"C1", "C4"}, "tyres": {"C2"}, "gap": set(), "nothing": set()}


class FakeModel:
    """Deterministic vectors: one dimension per letter a-z, plus a marker for the prompt,
    so query and document prompts are visible in the result."""

    def __init__(self):
        self.calls = []

    def encode(self, texts, prompt_name=None, normalize_embeddings=True):
        self.calls.append(prompt_name)
        out = []
        for t in texts:
            v = [0.0] * 28
            for ch in t.lower():
                if "a" <= ch <= "z":
                    v[ord(ch) - 97] += 1
            v[26 if prompt_name == "query" else 27] = 0.5
            out.append(v)
        return out


def toy_frozen(folder, cases=None, chunks=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    dev = json.dumps(cases or [dict(c, expected_fragments=[], expected=[]) for c in TOY_CASES]).encode()
    corpus = json.dumps({"chunks": chunks or TOY_CHUNKS}).encode()
    (folder / "dev.json").write_bytes(dev)
    (folder / "corpus.json").write_bytes(corpus)
    return {"dev.json": hashlib.sha256(dev).hexdigest(),
            "corpus.json": hashlib.sha256(corpus).hexdigest()}


class FrozenInputTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="m6b-frozen-"))

    def test_the_frozen_files_load_when_their_hashes_match(self):
        hashes = toy_frozen(self.dir)
        cases, chunks, got = harness.load_frozen(self.dir, hashes, TOY_LABELS)
        self.assertEqual([c["id"] for c in cases], [c["id"] for c in TOY_CASES])
        self.assertEqual(len(chunks), 6)
        self.assertEqual(got, hashes)

    def test_a_changed_file_is_refused(self):
        hashes = toy_frozen(self.dir)
        (self.dir / "dev.json").write_bytes(b"[]")
        with self.assertRaises(harness.FrozenInputError):
            harness.load_frozen(self.dir, hashes, TOY_LABELS)

    def test_a_case_list_that_is_not_the_labelled_one_is_refused(self):
        hashes = toy_frozen(self.dir)
        with self.assertRaises(harness.FrozenInputError):
            harness.load_frozen(self.dir, hashes, dict(TOY_LABELS, extra=set()))

    def test_a_label_that_is_not_a_chunk_is_refused(self):
        hashes = toy_frozen(self.dir)
        with self.assertRaises(harness.FrozenInputError):
            harness.load_frozen(self.dir, hashes, dict(TOY_LABELS, tea={"C99"}))

    def test_the_reserved_acceptance_cases_are_never_opened(self):
        hashes = toy_frozen(self.dir)
        reserved = self.dir / harness.RESERVED
        reserved.write_text("[]")
        os.chmod(reserved, 0)                  # opening it would raise
        try:
            harness.load_frozen(self.dir, hashes, TOY_LABELS)
        finally:
            os.chmod(reserved, 0o600)

    def test_the_d124_labels_are_frozen_as_approved(self):
        self.assertEqual({k: sorted(v) for k, v in harness.LABELS.items()}, {
            "local_stage": ["C19"], "client_policy": ["C9"], "write_approval": ["C19"],
            "media_storage": ["C11", "C22"], "query_navigation": ["C5"],
            "source_retention": [], "remote_voice": ["C20"], "french_voice": ["C20"],
            "unknown_gpu": [], "unknown_password": []})
        self.assertEqual(harness.REPORTED_APART, {"source_retention"})

    def test_the_archive_hashes_are_the_1_oct_ones(self):
        path = harness.FROZEN / "artifact_hashes.json"
        if not path.exists():
            self.skipTest("evidence archive not on this machine")
        archived = json.loads(path.read_text())
        for name, digest in harness.FROZEN_HASHES.items():
            self.assertEqual(archived[name], digest)


class LexicalArmTest(unittest.TestCase):
    def test_the_ranking_is_retrieval_v1s(self):
        path = harness.FROZEN / "retrieval.py"
        if not path.exists():
            self.skipTest("evidence archive not on this machine")
        archived = json.loads((harness.FROZEN / "artifact_hashes.json").read_text())
        self.assertEqual(harness.sha256_file(path), archived["retrieval.py"])
        spec = importlib.util.spec_from_file_location("retrieval_v1_archived", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)            # defines functions only; main() not called
        for case in TOY_CASES:
            with self.subTest(case=case["id"]):
                theirs = [(c["id"], c["score"]) for c in mod.retrieve(TOY_CHUNKS, case["question"], k=5)]
                self.assertEqual(harness.lexical_ranking(TOY_CHUNKS, case["question"])[:5], theirs)

    def test_only_matching_chunks_are_returned(self):
        self.assertEqual(harness.lexical_ranking(TOY_CHUNKS, "Zorblium quantelflax?"), [])


class EmbeddingArmTest(unittest.TestCase):
    def test_prompts_dimensions_and_ties(self):
        model = FakeModel()
        arm = harness.EmbeddingArm("B-toy", "toy/model", (None, 4), loader=lambda folder: model)
        arm.load()
        out = arm.rank(TOY_CHUNKS, TOY_CASES)
        self.assertEqual(sorted(out), ["B-toy@4", "B-toy@full"])
        self.assertEqual(model.calls, ["document", "query"])
        for key in out:
            for case in TOY_CASES:
                ranking = out[key][case["id"]]
                self.assertEqual(sorted(cid for cid, _ in ranking), [c["id"] for c in TOY_CHUNKS])
                scores = [s for _, s in ranking]
                self.assertEqual(scores, sorted(scores, reverse=True))

    def test_truncation_renormalises(self):
        ranking = harness.cosine_ranking([3.0, 4.0, 100.0], [[3.0, 4.0, -100.0], [4.0, 3.0, 0.0]],
                                         ["X", "Y"], dim=2)
        self.assertEqual(ranking[0], ("X", 1.0))
        # Sorted on full precision: a difference below the saved rounding still orders them.
        close = harness.cosine_ranking([1.0, 0.0], [[1.0, 1e-4], [1.0, 2e-4]], ["A", "B"])
        self.assertEqual([cid for cid, _ in close], ["A", "B"])
        close = harness.cosine_ranking([1.0, 0.0], [[1.0, 2e-4], [1.0, 1e-4]], ["A", "B"])
        self.assertEqual([cid for cid, _ in close], ["B", "A"])
        self.assertEqual(harness.cosine_ranking([1.0, 0.0], [[1.0, 0.0], [1.0, 0.0]], ["B", "A"])[0][0], "A")

    def test_model_files_are_checked_against_the_manifest(self):
        folder = Path(tempfile.mkdtemp(prefix="m6b-model-"))
        (folder / "w.bin").write_bytes(b"weights")
        entry = {"repository": "toy/model", "revision": "0" * 40,
                 "files": [{"path": "w.bin", "size": 7,
                            "sha256": hashlib.sha256(b"weights").hexdigest()}]}
        self.assertEqual(harness.verify_model(entry, folder), folder)
        (folder / "w.bin").write_bytes(b"weightz")
        with self.assertRaises(harness.FrozenInputError):
            harness.verify_model(entry, folder)
        arm = harness.EmbeddingArm("B-toy", "toy/model", (None,), loader=lambda f: FakeModel(),
                                   folder=folder, manifest_entry=entry)
        with self.assertRaises(harness.FrozenInputError):
            arm.load()


class NoNetworkTest(unittest.TestCase):
    def test_every_python_route_raises_inside_and_works_after(self):
        before = socket.socket()
        try:
            with harness.NoNetwork():
                for call in (lambda: socket.getaddrinfo("localhost", 80),
                             lambda: socket.gethostbyname("localhost"),
                             lambda: socket.gethostbyname_ex("localhost"),
                             lambda: socket.gethostbyaddr("127.0.0.1"),
                             lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
                             lambda: socket.create_connection(("127.0.0.1", 9)),
                             lambda: socket.socket(),
                             lambda: before.connect(("127.0.0.1", 9)),
                             lambda: before.sendto(b"x", ("127.0.0.1", 9))):
                    with self.assertRaises(harness.NetworkBlocked):
                        call()
        finally:
            before.close()
        self.assertTrue(socket.gethostbyname("localhost"))
        socket.socket().close()

    def test_the_probe_sees_no_sandbox_here(self):
        self.assertFalse(harness.network_denied())

    def test_a_worker_outside_the_sandbox_refuses_to_start(self):
        d = Path(tempfile.mkdtemp(prefix="m6b-frozen-"))
        hashes = toy_frozen(d)
        with self.assertRaises(harness.NotSandboxed):
            harness.worker(harness.LexicalArm(), d, hashes, TOY_LABELS, d / "out.json")
        self.assertFalse((d / "out.json").exists())

    def test_ranking_runs_inside_the_block(self):
        class Probe(FakeModel):
            def encode(self, texts, **kw):
                socket.getaddrinfo("example.com", 443)
        d = Path(tempfile.mkdtemp(prefix="m6b-frozen-"))
        hashes = toy_frozen(d)
        arm = harness.EmbeddingArm("B-probe", "toy/model", (None,), loader=lambda f: Probe())
        with self.assertRaises(harness.NetworkBlocked):
            harness.worker(arm, d, hashes, TOY_LABELS, d / "out.json", require_sandbox=False)


class MetricsTest(unittest.TestCase):
    def test_recall_and_wrong_hits(self):
        row = harness.score_case("tea", [("C4", .9), ("C5", .8), ("C6", .7), ("C1", .6)], TOY_LABELS)
        self.assertEqual((row["recall@3"], row["recall@5"], row["wrong_top3"]), (1, 1, 2))
        row = harness.score_case("tyres", [("C5", .9), ("C6", .8), ("C1", .7), ("C3", .6), ("C2", .5)],
                                 TOY_LABELS)
        self.assertEqual((row["recall@3"], row["recall@5"], row["wrong_top3"]), (0, 1, 3))
        row = harness.score_case("nothing", [], TOY_LABELS)
        self.assertEqual((row["recall@3"], row["wrong_top3"]), (None, 0))
        row = harness.score_case("gap", [("C1", .1), ("C2", .1)], TOY_LABELS)
        self.assertEqual((row["recall@3"], row["wrong_top3"]), (None, 2))

    def test_a_case_reported_apart_stays_out_of_the_totals(self):
        rows = [harness.score_case("local_stage", [("C19", 1.0)]),
                harness.score_case("source_retention", [("C1", 1.0), ("C2", .5), ("C3", .1)]),
                harness.score_case("unknown_gpu", [("C7", 1.0)])]
        self.assertTrue(rows[1]["reported_apart"])
        t = harness.totals(rows)
        self.assertEqual((t["cases"], t["answerable"], t["recall@3"], t["wrong_top3"],
                          t["wrong_top3_answerable"], t["wrong_top3_unanswerable"]),
                         (2, 1, 1, 1, 0, 1))


class RunTest(unittest.TestCase):
    def setUp(self):
        self.frozen = Path(tempfile.mkdtemp(prefix="m6b-frozen-"))
        self.hashes = toy_frozen(self.frozen)
        self.out = Path(tempfile.mkdtemp(prefix="m6b-run-")) / "run"
        self.loads = []

    def in_process(self, name, frozen, hashes, labels, out_file):
        """A stand-in for the sandboxed process: toy arms, same worker code."""
        model_loads = self.loads

        def loader(folder):
            model_loads.append(folder)
            return FakeModel()
        arm = harness.LexicalArm() if name == "A" else \
            harness.EmbeddingArm("B-toy", "toy/model", (None, 4), loader=loader)
        harness.worker(arm, frozen, hashes, labels, out_file, require_sandbox=False)

    def test_a_run_saves_every_ranking_with_hashes(self):
        summary = harness.run(("A", "B"), self.out, self.frozen, self.hashes, TOY_LABELS,
                              spawn=self.in_process, require_sandboxed=False)
        self.assertEqual(sorted(summary["arms"]), ["A-lexical", "B-toy@4", "B-toy@full"])
        self.assertEqual(summary["inputs"]["labels"], harness.labels_digest(TOY_LABELS))
        hashes = json.loads((self.out / "hashes.json").read_text())
        self.assertEqual(sorted(hashes), ["rankings-A-lexical.json", "rankings-B-toy-4.json",
                                          "rankings-B-toy-full.json", "scores.json",
                                          "worker-A.json", "worker-B.json"])
        for name, digest in hashes.items():
            self.assertEqual(harness.sha256_file(self.out / name), digest)
        lex = json.loads((self.out / "rankings-A-lexical.json").read_text())["rankings"]
        self.assertEqual(lex["tea"][0][0], "C1")
        for arm in ("A-lexical", "B-toy"):
            self.assertIn("peak_rss_bytes", summary["load"][arm])
        self.assertEqual(len(self.loads), 2)             # cold, then warm after release

    def test_an_existing_folder_is_refused(self):
        self.out.mkdir(parents=True)
        with self.assertRaises(harness.outpath.OutsideVaultError):
            harness.run(("A",), self.out, self.frozen, self.hashes, TOY_LABELS, spawn=self.in_process,
                        require_sandboxed=False)

    def test_worker_output_that_breaks_the_rules_is_refused(self):
        def bad(change):
            def spawn(name, frozen, hashes, labels, out_file):
                self.in_process(name, frozen, hashes, labels, out_file)
                data = json.loads(out_file.read_text())
                change(data)
                out_file.write_text(json.dumps(data))
            return spawn
        cases = [
            (lambda d: None, harness.NotSandboxed, True),                  # in-process: not sandboxed
            (lambda d: d["variants"]["A-lexical"].pop("tea"), harness.FrozenInputError, False),
            (lambda d: d["variants"].update({"../../x@1": {}}), harness.FrozenInputError, False),
            (lambda d: d["variants"]["A-lexical"]["tea"].append(["C99", 1.0]),
             harness.FrozenInputError, False),
            (lambda d: d.update(inputs={}), harness.FrozenInputError, False),
        ]
        for i, (change, error, sandboxed) in enumerate(cases):
            with self.subTest(i=i):
                out = self.out.parent / ("bad-%d" % i)
                with self.assertRaises(error):
                    harness.run(("A",), out, self.frozen, self.hashes, TOY_LABELS,
                                spawn=bad(change), require_sandboxed=sandboxed)
                self.assertFalse((out / "scores.json").exists())

    def test_a_worker_never_overwrites_a_file(self):
        target = self.out.parent / "exists.json"
        target.write_text("{}")
        with self.assertRaises(FileExistsError):
            harness.worker(harness.LexicalArm(), self.frozen, self.hashes, TOY_LABELS, target,
                           require_sandbox=False)
        self.assertEqual(target.read_text(), "{}")

    def test_the_lexical_arm_end_to_end_in_the_real_sandbox(self):
        summary = harness.run(("A",), self.out, self.frozen, self.hashes, TOY_LABELS)
        self.assertTrue(summary["load"]["A-lexical"]["sandboxed"])
        self.assertEqual(summary["arms"]["A-lexical"]["totals"]["recall@3"], 2)

    def test_arm_b_is_loaded_text_only_and_c_in_full(self):
        seen = []
        saved = harness._sentence_transformer
        harness._sentence_transformer = lambda folder, config_kwargs=None: seen.append(config_kwargs)
        try:
            for name in ("B", "C"):
                arm = harness.arm_by_name(name)
                arm.loader(Path("/nonexistent"))
        finally:
            harness._sentence_transformer = saved
        self.assertEqual(seen, [{"vision_config": None, "audio_config": None}, None])
        self.assertEqual(harness.arm_by_name("B").dims, (768, 256))
        self.assertEqual(harness.arm_by_name("C").dims, (None,))


if __name__ == "__main__":
    unittest.main()
