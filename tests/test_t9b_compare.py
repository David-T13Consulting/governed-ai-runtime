"""T9b step 3: the runner's protocol shape, on synthetic blocks only (no model, no server).

What must hold (brief T9b, Protocol): 7 blocks C2 TWA TWB C2 TWB TWA C2; C2 scored at 1 and
7, position 4 a drift check only; each TW configuration scored twice (n = 20); the three
configurations differ from T9 only in the thinking switch, the token cap and the timeout;
T9's own runner keeps its behaviour.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "experiments"))

import t9_compare as t9  # noqa: E402
import t9b_compare as t9b  # noqa: E402

from dbrain import adapter, answer  # noqa: E402


class FakeRun(t9b.Run):
    """Blocks are records with a preset p50 per (position, attempt at that position)."""

    def __init__(self, p50s=None):
        super().__init__("test", 0, {k: {"file": k} for k in t9b.CONFIGS}, Path("/nonexistent"), {})
        self.p50s = p50s or {}
        self.calls = []

    def write(self, name, obj):
        pass  # nothing leaves the test

    def block(self, position, key, attempt_note="", warmup_only=False):
        self.calls.append((position, key, attempt_note))
        seen = sum(1 for p, _, _ in self.calls if p == position)
        p50 = self.p50s.get((position, seen), 1.0)
        return self._done({"attempt": len(self.blocks) + 1, "position": position, "model": key,
                           "status": "ok", "p50": p50, "latencies": [p50] * 10,
                           "peak_rss_kib": 1048576})


class ShapeTest(unittest.TestCase):
    def test_block_order_and_scored_positions(self):
        self.assertEqual(t9b.ORDER, ["C2", "TWA", "TWB", "C2", "TWB", "TWA", "C2"])
        self.assertEqual([i for i, k in enumerate(t9b.ORDER, 1) if k == "C2"], [1, 4, 7])
        self.assertEqual(t9b.SCORED_BASELINE_POSITIONS, (1, 7))
        self.assertEqual(t9b.DRIFT_POSITIONS, (4, 7))

    def test_each_configuration_scores_twenty_and_c2_skips_position_4(self):
        run = FakeRun()
        out = run.go()
        self.assertFalse(out["drift_affected"])
        for key in ("C2", "TWA", "TWB"):
            self.assertEqual(out["models"][key]["n"], 20, key)
        c2_positions = [run.blocks[i - 1]["position"] for i in out["models"]["C2"]["blocks"]]
        self.assertEqual(c2_positions, [1, 7])
        twa = [run.blocks[i - 1]["position"] for i in out["models"]["TWA"]["blocks"]]
        self.assertEqual(twa, [2, 6])

    def test_drift_at_position_4_re_runs_the_blocks_since_the_last_baseline(self):
        run = FakeRun({(4, 1): 2.0})  # 100 % off position 1's p50 of 1.0; the re-run is fine
        out = run.go()
        self.assertEqual([c[:2] for c in run.calls],
                         [(1, "C2"), (2, "TWA"), (3, "TWB"), (4, "C2"),
                          (2, "TWA"), (3, "TWB"), (4, "C2"),
                          (5, "TWB"), (6, "TWA"), (7, "C2")])
        self.assertFalse(out["drift_affected"])

    def test_drift_that_persists_marks_the_run(self):
        run = FakeRun({(7, 1): 2.0, (7, 2): 2.0})
        self.assertTrue(run.go()["drift_affected"])


class ConfigurationTest(unittest.TestCase):
    def test_flags_differ_from_t9_only_in_the_thinking_switch(self):
        before = list(t9.SERVER_FLAGS)
        for key, want in (("C2", "off"), ("TWA", "on"), ("TWB", "off")):
            flags = t9b.server_flags(key)
            i = flags.index("--reasoning")
            self.assertEqual(flags[i + 1], want)
            self.assertEqual(flags[:i + 1] + flags[i + 2:], before[:i + 1] + before[i + 2:])
        self.assertEqual(t9.SERVER_FLAGS, before)  # not mutated

    def test_caps_timeouts_and_signatures(self):
        self.assertEqual({k: (c["max_tokens"], c["timeout"]) for k, c in t9b.CONFIGS.items()},
                         {"C2": (320, 120), "TWA": (2048, 300), "TWB": (320, 120)})
        t9_sig = adapter.request_signature(answer.PROMPT)
        self.assertEqual(t9b.signature("C2"), t9_sig)
        self.assertEqual(t9b.signature("TWB"), t9_sig)
        self.assertNotEqual(t9b.signature("TWA"), t9_sig)

    def test_the_child_gets_its_configuration(self):
        run = FakeRun()
        run.manifest = {"request_signatures": {"TWA": "abc"}}
        argv = run.child_argv("TWA", Path("/tmp/r.json"), False)
        self.assertEqual(argv[-2:], ["--config", "TWA"])
        self.assertEqual(run.request_signature_for("TWA"), "abc")
        with mock.patch.object(t9, "child", return_value=0) as c:
            t9b.child("r.json", "TWA", True)
        c.assert_called_once_with("r.json", True, max_tokens=2048, timeout=300,
                                  extra_code=[Path(t9b.__file__)])

    def test_the_capped_adapter_sends_its_cap(self):
        with mock.patch.object(adapter.LlamaCppAdapter, "generate",
                               return_value=("ok", 0.0)) as gen:
            model = t9.CappedAdapter(2048, endpoint="http://127.0.0.1:%d" % t9.PORT, timeout=300)
            model.generate("prompt")
        gen.assert_called_once_with("prompt", 2048)
        self.assertEqual(model.timeout, 300)

    def test_a_signature_mismatch_voids_the_run(self):
        run = FakeRun()
        run.manifest = {"code": "c", "request_signatures": {"TWA": "abc"}, "cases_pin": "p",
                        "case_order": [], "corpus": {"version": "v", "files": {}}}
        data = {"code": "c", "request_signature": "xyz", "cases_pin": "p", "case_order": [],
                "corpus_version": "v", "warmup": {}}
        self.assertIn("request signature differs from the manifest",
                      run.version_problems(data, True, "TWA"))


class T9UnchangedTest(unittest.TestCase):
    def test_t9_keeps_its_shape(self):
        self.assertEqual(t9.Run.order, ["B0", "C1", "C2", "C3", "B0", "C3", "C2", "C1", "B0"])
        self.assertEqual((t9.Run.drift_positions, t9.Run.scored_baseline_positions,
                          t9.Run.baseline_key), ((5, 9), (1, 9), "B0"))
        run = t9.Run("x", 0, {}, Path("/nonexistent"), {"request_signature": "s"})
        self.assertEqual(run.request_signature_for("C1"), "s")
        argv = run.child_argv("C1", Path("/tmp/r.json"), True)
        self.assertEqual(argv[1:], [str(Path(t9.__file__).resolve()), "--child", "/tmp/r.json",
                                    "--warmup-only"])

    def test_t9_code_hashes_are_unchanged_without_extra(self):
        self.assertEqual(list(t9.code_hashes())[:2], ["experiments/t9_compare.py", "run_dev_cases.py"])
        self.assertIn("experiments/t9b_compare.py", t9b.code_hashes())


if __name__ == "__main__":
    unittest.main()
