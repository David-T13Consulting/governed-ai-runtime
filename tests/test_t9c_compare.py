"""T9c step 3: the runner's protocol shape, on synthetic blocks only (no model, no server).

What must hold (brief T9c, Q144): 7 blocks C2 G12 G26 C2 G26 G12 C2; C2 scored at 1 and 7,
position 4 a drift check only; each candidate scored twice (n = 20); the three
configurations use T9's server flags unchanged (thinking off) and T9's request signature,
and differ only in the model file and the timeout; T9 and T9b keep their behaviour.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "experiments"))

import t9_compare as t9  # noqa: E402
import t9b_compare as t9b  # noqa: E402
import t9c_compare as t9c  # noqa: E402

from dbrain import adapter, answer  # noqa: E402


class FakeRun(t9c.Run):
    def __init__(self, p50s=None):
        super().__init__("test", 0, {k: {"file": k} for k in t9c.CONFIGS}, Path("/nonexistent"), {})
        self.p50s = p50s or {}
        self.calls = []

    def write(self, name, obj):
        pass

    def block(self, position, key, attempt_note="", warmup_only=False):
        self.calls.append((position, key, attempt_note))
        seen = sum(1 for p, _, _ in self.calls if p == position)
        p50 = self.p50s.get((position, seen), 1.0)
        return self._done({"attempt": len(self.blocks) + 1, "position": position, "model": key,
                           "status": "ok", "p50": p50, "latencies": [p50] * 10,
                           "peak_rss_kib": 1048576})


class ShapeTest(unittest.TestCase):
    def test_block_order_and_scored_positions(self):
        self.assertEqual(t9c.ORDER, ["C2", "G12", "G26", "C2", "G26", "G12", "C2"])
        self.assertEqual(t9c.SCORED_BASELINE_POSITIONS, (1, 7))
        self.assertEqual(t9c.DRIFT_POSITIONS, (4, 7))

    def test_each_configuration_scores_twenty(self):
        run = FakeRun()
        out = run.go()
        self.assertFalse(out["drift_affected"])
        for key in ("C2", "G12", "G26"):
            self.assertEqual(out["models"][key]["n"], 20, key)
        pos = lambda k: [run.blocks[i - 1]["position"] for i in out["models"][k]["blocks"]]
        self.assertEqual((pos("C2"), pos("G12"), pos("G26")), ([1, 7], [2, 6], [3, 5]))

    def test_drift_at_position_4_re_runs_since_the_last_baseline(self):
        run = FakeRun({(4, 1): 2.0})
        out = run.go()
        self.assertEqual([c[:2] for c in run.calls],
                         [(1, "C2"), (2, "G12"), (3, "G26"), (4, "C2"),
                          (2, "G12"), (3, "G26"), (4, "C2"),
                          (5, "G26"), (6, "G12"), (7, "C2")])
        self.assertFalse(out["drift_affected"])

    def test_drift_that_persists_marks_the_run(self):
        self.assertTrue(FakeRun({(7, 1): 2.0, (7, 2): 2.0}).go()["drift_affected"])


class ConfigurationTest(unittest.TestCase):
    def test_flags_are_t9s_unchanged_for_every_configuration(self):
        before = list(t9.SERVER_FLAGS)
        for key in t9c.CONFIGS:
            self.assertEqual(t9c.server_flags(key), before, key)
        self.assertEqual(t9.SERVER_FLAGS, before)

    def test_caps_timeouts_and_signatures(self):
        self.assertEqual({k: (c["max_tokens"], c["timeout"]) for k, c in t9c.CONFIGS.items()},
                         {"C2": (320, 120), "G12": (320, 300), "G26": (320, 300)})
        t9_sig = adapter.request_signature(answer.PROMPT)
        for key in t9c.CONFIGS:
            self.assertEqual(t9c.signature(key), t9_sig, key)

    def test_the_child_gets_its_configuration(self):
        run = FakeRun()
        run.manifest = {"request_signatures": {"G26": "abc"}}
        self.assertEqual(run.child_argv("G26", Path("/tmp/r.json"), False)[-2:], ["--config", "G26"])
        self.assertEqual(run.request_signature_for("G26"), "abc")
        with mock.patch.object(t9, "child", return_value=0) as c:
            t9c.child("r.json", "G26", True)
        c.assert_called_once_with("r.json", True, max_tokens=320, timeout=300,
                                  extra_code=[Path(t9c.__file__)])

    def test_the_pre_download_manifest_pins_all_three(self):
        m = json.loads((HERE / "artifacts" / "t9c-2026-10-21" / "manifest-pre-download.json")
                       .read_text())["candidates"] if (HERE / "artifacts" / "t9c-2026-10-21").exists() else None
        if m is None:
            self.skipTest("artifacts are git-ignored; the manifest exists only on the Air")
        self.assertEqual(sorted(m), ["C2", "G12", "G26"])
        for key, c in m.items():
            self.assertEqual(len(c["sha256"]), 64, key)
            self.assertTrue(c["file"].endswith(".gguf"), key)
        self.assertEqual(m["G12"]["license"], "apache-2.0")
        self.assertEqual(m["G26"]["license"], "apache-2.0")


class PinRefusalTest(unittest.TestCase):
    """Review 1/2 should-fix: a wrong size or hash stops the run before any block, on
    synthetic files in a temporary folder (no real model, no server)."""

    def setUp(self):
        import hashlib, tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        (tmp / "models").mkdir()
        runtime = tmp / "llama-server"
        runtime.write_bytes(b"runtime")
        self.files = {}
        cands = {}
        for key, data in (("C2", b"c2"), ("G12", b"g12"), ("G26", b"g26")):
            f = tmp / "models" / ("%s.gguf" % key)
            f.write_bytes(data)
            cands[key] = {"file": f.name, "sha256": hashlib.sha256(data).hexdigest(),
                          "size": len(data), "repo": "r", "revision": "v", "license": "apache-2.0"}
        self.manifest = tmp / "m.json"
        self.cands = cands
        self.tmp = tmp
        self.patches = [mock.patch.object(t9, "APP", tmp), mock.patch.object(t9, "RUNTIME", runtime),
                        mock.patch.object(t9, "RUNTIME_PIN", hashlib.sha256(b"runtime").hexdigest()),
                        mock.patch.object(t9, "verify_checkout", return_value="head"),
                        mock.patch.object(t9c, "Run")]
        for pt in self.patches:
            pt.start()
            self.addCleanup(pt.stop)

    def run_main(self):
        self.manifest.write_text(json.dumps({"candidates": self.cands}))
        return t9c.main(["--label", "x", "--manifest", str(self.manifest), "--commit", "head"])

    def test_wrong_size_stops_before_any_block(self):
        self.cands["G26"]["size"] += 1
        with self.assertRaises(SystemExit) as ctx:
            self.run_main()
        self.assertIn("G26 does not match its pinned size", str(ctx.exception))
        t9c.Run.assert_not_called()

    def test_wrong_hash_stops_before_any_block(self):
        self.cands["G12"]["sha256"] = "0" * 64
        with self.assertRaises(SystemExit) as ctx:
            self.run_main()
        self.assertIn("G12 does not match its pin", str(ctx.exception))
        t9c.Run.assert_not_called()

    def test_a_changed_runtime_stops_before_any_block(self):
        with mock.patch.object(t9, "RUNTIME_PIN", "0" * 64):
            with self.assertRaises(SystemExit) as ctx:
                self.run_main()
        self.assertIn("runtime does not match its pin", str(ctx.exception))
        t9c.Run.assert_not_called()


class EarlierRunnersUnchangedTest(unittest.TestCase):
    def test_t9_and_t9b_keep_their_shape(self):
        self.assertEqual(t9.Run.order, ["B0", "C1", "C2", "C3", "B0", "C3", "C2", "C1", "B0"])
        self.assertEqual(t9b.ORDER, ["C2", "TWA", "TWB", "C2", "TWB", "TWA", "C2"])
        self.assertIn("experiments/t9c_compare.py", t9c.code_hashes())
        self.assertNotIn("experiments/t9c_compare.py", t9b.code_hashes())


if __name__ == "__main__":
    unittest.main()
