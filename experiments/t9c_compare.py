#!/usr/bin/env python3
"""T9c runner: two larger Gemma 4 models beside the C2 default (brief `T9c-larger-local-model`,
candidates, disk and window chosen by David, Q144, 21 Oct).

T9's runner (`t9_compare.py`) on T9b's pattern, with these declared differences only:
7 blocks in the order C2 G12 G26 C2 G26 G12 C2 (C2 scored at positions 1 and 7, position 4
a drift check only, T9's 25 % rule and re-run rule); three configurations on the one pinned
build that differ only in the model file and the request timeout:

    C2   Gemma 4 E4B-it QAT Q4_0 (the default), thinking off, 320 tokens, 120 s (T9's)
    G12  Gemma 4 12B-it QAT Q4_0,                thinking off, 320 tokens, 300 s
    G26  Gemma 4 26B-A4B-it QAT Q4_0,            thinking off, 320 tokens, 300 s

The larger models get T9b's longer timeout because they are slower; a timeout is an outcome
with its elapsed time, never a crash. Everything else is T9's: sandbox, flags, cool-down,
cases, the frozen corpus (878a08ea6f57, PL1 step 2), pins, RSS sampling, the write guard.

    python3 experiments/t9c_compare.py --label 2026-10-21-a --commit <sha> \\
        --manifest artifacts/t9c-2026-10-21/manifest-pre-download.json
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "experiments"))

from dbrain import adapter as adapter_mod  # noqa: E402
from dbrain import answer, config, index, outpath  # noqa: E402

import run_dev_cases  # noqa: E402
import t9_compare as t9  # noqa: E402

ORDER = ["C2", "G12", "G26", "C2", "G26", "G12", "C2"]
DRIFT_POSITIONS = (4, 7)
SCORED_BASELINE_POSITIONS = (1, 7)
# key -> the pre-download manifest's candidate, token cap, request timeout (s). Thinking is
# off for all three (T9's server flags, unchanged).
CONFIGS = {
    "C2": {"pin": "C2", "max_tokens": 320, "timeout": 120},
    "G12": {"pin": "G12", "max_tokens": 320, "timeout": 300},
    "G26": {"pin": "G26", "max_tokens": 320, "timeout": 300},
}


def server_flags(key):
    """T9's flags, unchanged for every configuration (thinking off)."""
    flags = list(t9.SERVER_FLAGS)
    assert flags[flags.index("--reasoning") + 1] == "off"
    return flags


def signature(key):
    return adapter_mod.request_signature(answer.PROMPT, CONFIGS[key]["max_tokens"])


def code_hashes():
    return t9.code_hashes(extra=[Path(__file__)])


class Run(t9.Run):
    order = ORDER
    drift_positions = DRIFT_POSITIONS
    scored_baseline_positions = SCORED_BASELINE_POSITIONS
    baseline_key = "C2"

    def start_server(self, key, log):
        return subprocess.Popen(self.sandboxed_argv([str(t9.RUNTIME), "-m", str(self.models[key]["path"])]
                                                    + server_flags(key)),
                                stdout=log, stderr=subprocess.STDOUT, env=t9.clean_env(), cwd=str(HERE))

    def request_signature_for(self, key):
        return self.manifest["request_signatures"][key]

    def child_argv(self, key, report, warmup_only):
        return ([sys.executable, str(Path(__file__).resolve()), "--child", str(report), "--config", key]
                + (["--warmup-only"] if warmup_only else []))


def child(report_path, key, warmup_only=False):
    c = CONFIGS[key]
    return t9.child(report_path, warmup_only, max_tokens=c["max_tokens"], timeout=c["timeout"],
                    extra_code=[Path(__file__)])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--config", choices=sorted(CONFIGS), help=argparse.SUPPRESS)
    ap.add_argument("--warmup-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--label")
    ap.add_argument("--cooldown", type=float, default=t9.COOLDOWN)
    ap.add_argument("--manifest", help="the T9c pre-download manifest")
    ap.add_argument("--commit", help="dbrain-runtime commit the run uses (checked against HEAD)")
    ap.add_argument("--dry-check", action="store_true",
                    help="pins, network probe, and each configuration's load and warm-up only")
    args = ap.parse_args(argv)
    if args.child:
        if not args.config:
            ap.error("--child needs --config")
        return child(args.child, args.config, args.warmup_only)
    if not (args.label and args.manifest and args.commit):
        ap.error("--label, --manifest and --commit are required")
    if args.cooldown != t9.COOLDOWN and not args.dry_check:
        raise SystemExit("the protocol's cool-down is %d s; other values are refused" % t9.COOLDOWN)
    if not t9.LABEL.match(args.label):
        raise SystemExit("label must be letters, digits, '.', '_' or '-' only")

    head = t9.verify_checkout(args.commit)
    pre = json.loads(Path(args.manifest).read_text())["candidates"]
    models = {}
    for key, c in CONFIGS.items():
        p = pre[c["pin"]]
        models[key] = {"file": p["file"], "sha256": p["sha256"], "size": p["size"],
                       "repo": p["repo"], "revision": p["revision"],
                       "path": t9.APP / "models" / p["file"]}
    pins = {"runtime": {"path": str(t9.RUNTIME), "sha256": t9.sha256(t9.RUNTIME)}}
    if pins["runtime"]["sha256"] != t9.RUNTIME_PIN:
        raise SystemExit("runtime does not match its pin")
    for key, m in models.items():
        if m["path"].stat().st_size != m["size"]:
            raise SystemExit("%s does not match its pinned size" % key)
        got = t9.sha256(m["path"])
        if got != m["sha256"]:
            raise SystemExit("%s does not match its pin: %s" % (key, got))
        pins[key] = {k: (str(v) if isinstance(v, Path) else v) for k, v in m.items()}

    # Evidence only under this repository's artifacts/ folder (the brief's output boundary).
    root = (HERE / "artifacts").resolve()
    evidence = root / "t9c" / ("%s-%s" % ("dry" if args.dry_check else "run", args.label))
    evidence = Path(outpath.make_dir(evidence)).resolve()
    if root not in evidence.parents:
        raise SystemExit("evidence folder escaped artifacts/: %s" % evidence)

    profile = config.DEVCASES
    chunks, files = index.build(profile)
    cases = run_dev_cases.load_dev_cases()
    if len(cases) != t9.CASES_PER_BLOCK:
        raise SystemExit("expected %d dev cases, found %d" % (t9.CASES_PER_BLOCK, len(cases)))
    manifest = {
        "brief": "90 System/briefs/T9c-larger-local-model.md",
        "runtime_commit": head,
        "runner_sha256": t9.sha256(Path(__file__)),
        "code": code_hashes(),
        "corpus": {"profile": profile.name, "files": files,
                   "version": run_dev_cases.corpus_version(chunks)},
        "request_signatures": {key: signature(key) for key in CONFIGS},
        "case_order": [c["id"] for c in cases],
        "cases_pin": t9.cases_pin(cases),
        "warmup_question": t9.WARMUP,
        "configs": CONFIGS,
        "server_flags": {key: server_flags(key) for key in CONFIGS},
        "server_sandboxed": True,
        "block_order": ORDER,
        "scored_baseline_positions": SCORED_BASELINE_POSITIONS,
        "drift_positions": DRIFT_POSITIONS,
        "cooldown_seconds": args.cooldown,
        "protocol_cooldown": args.cooldown == t9.COOLDOWN,
        "drift_limit": t9.DRIFT_LIMIT,
        "pins": pins,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    run = Run(args.label, args.cooldown, models, evidence, manifest)
    run.write("sandbox-nonet.sb", t9.SANDBOX)
    net = t9.network_denied(run.sandbox)
    manifest["network_check"] = net
    run.write("manifest-pre-run.json", manifest)
    if not net["denied"]:
        raise SystemExit("could not prove the sandbox denies outbound connections; stopping: %s" % net)

    if args.dry_check:
        run.cooldown = 0
        done = {key: run.blocks[run.block(0, key, "dry check: warm-up only, no dev case run",
                                          warmup_only=True)] for key in CONFIGS}
        print(json.dumps({key: {k: b.get(k) for k in ("status", "problems", "warmup",
                                                     "warmup_seconds", "peak_rss_kib_whole_block")}
                          for key, b in done.items()}, indent=1))
        return 0 if all(b.get("status") == "ok" for b in done.values()) else 1
    print(json.dumps(run.go(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
