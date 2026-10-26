#!/usr/bin/env python3
"""T9/M6 local model comparison runner (brief `T9-M6-model-comparison`, protocol frozen 5 Oct).

Runs the frozen protocol and nothing else: 9 blocks in the order B0 C1 C2 C3 B0 C3 C2 C1 B0,
180 s idle before each, one unscored warm-up then the 10 dev cases per block, server RSS
sampled every 0.25 s from launch to stop, machine state before and after each block, the
drift rule, and nearest-rank p50/p95 over each model's scored blocks. Judging the answers
(safety gate and the quality score) is not done here: the lead and Codex judge the saved
answers independently afterwards, as the brief requires.

    python3 experiments/t9_compare.py --label 2026-10-09-a --commit <sha> \\
        --manifest artifacts/t9-2026-10-06/manifest-pre-download.json

Run it from a clean checkout of the committed pipeline: the commit is checked against HEAD
and tracked files must be unmodified. The model server, the warm-up and the dev cases all
run under the no-network sandbox with provider keys unset; the server listens on loopback.
Evidence goes only under this repository's `artifacts/` folder, through the write guard.
`--dry-check` stops after the pins, the network probe, and each model's load and warm-up.
"""
import argparse
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from dbrain import adapter as adapter_mod  # noqa: E402
from dbrain import answer, config, index, outpath  # noqa: E402

import run_dev_cases  # noqa: E402

ORDER = ["B0", "C1", "C2", "C3", "B0", "C3", "C2", "C1", "B0"]
DRIFT_POSITIONS = (5, 9)          # baselines compared with the first one
SCORED_BASELINE_POSITIONS = (1, 9)
DRIFT_LIMIT = 0.25
COOLDOWN = 180.0                  # frozen; any other value marks the run non-protocol
CASE_TIMEOUT = 120                # seconds, per request (adapter timeout)
CASES_PER_BLOCK = 10
PORT = 18089                      # not the live MVP service's 18087, not the smoke test's 18088
PROBE_PORT = 18090                # loopback port outside the sandbox allow-list (denial probe)
WARMUP = "In one sentence, what is a second brain?"
PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "MISTRAL_API_KEY", "COHERE_API_KEY", "HF_TOKEN")
APP = Path.home() / "Library/Application Support/dbrain"
RUNTIME = APP / "llama/llama-b11146/llama-server"
RUNTIME_PIN = "41df13c126456f8e5fab2057c86a790067a85ea1dfd8fbc0071cc45fbba56262"
BASELINE = {"file": "Qwen3-4B-Q4_K_M.gguf",
            "sha256": "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"}
SERVER_FLAGS = ["--host", "127.0.0.1", "--port", str(PORT), "--offline", "--no-webui",
                "-np", "1", "-c", "4096", "-t", "4", "-ngl", "99", "--no-cache-prompt",
                # "Thinking off where the model has it" (protocol). Qwen3.5 ignores the
                # request's /no_think and spends the whole token cap thinking (probe, 6 Oct);
                # the server flag turns it off. Applied to every model, so all four run
                # under one set of flags; for models without thinking it changes nothing.
                "--reasoning", "off"]
SANDBOX = ("(version 1)\n(allow default)\n(deny network-outbound)\n"
           "(allow network-outbound (remote ip \"localhost:%d\"))\n"
           "(allow network-outbound (remote unix-socket))\n" % PORT)
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def nearest_rank(values, q):
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def code_hashes(extra=()):
    """The executed pipeline: this runner, the dev-case loader and every dbrain module, plus
    `extra` (a runner built on this one, T9b)."""
    files = ([Path(__file__).resolve(), HERE / "run_dev_cases.py"] + [Path(f).resolve() for f in extra]
             + sorted((HERE / "dbrain").glob("*.py")))
    return {str(f.relative_to(HERE)): sha256(f) for f in files}


def cases_pin(cases):
    """Hash of the complete dev cases (questions, fragments, expectations), in order."""
    return hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def clean_env():
    env = {k: v for k, v in os.environ.items() if k not in PROVIDER_KEYS}
    env["PYTHONPATH"] = str(HERE)
    return env


def machine_state():
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception as exc:  # recorded, never fatal
            return "unavailable: %s" % type(exc).__name__
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "load": run(["sysctl", "-n", "vm.loadavg"]),
            "memory_pressure": run(["memory_pressure"]).splitlines()[-1:],
            "top5_cpu": run(["ps", "-Ao", "pcpu,comm", "-r"]).splitlines()[:6]}


# --- child mode: one block's warm-up and cases, run inside the sandbox ------------------

class CappedAdapter(adapter_mod.LlamaCppAdapter):
    """T9b: one token cap for every request (answer.ask calls generate with the default)."""

    def __init__(self, cap, **kw):
        super().__init__(**kw)
        self.cap = cap

    def generate(self, prompt, max_tokens=320):
        return super().generate(prompt, self.cap)


def child(report_path, warmup_only=False, max_tokens=320, timeout=CASE_TIMEOUT, extra_code=()):
    """Warm-up, then the 10 dev cases in order. Every case gets a row with its elapsed time,
    whatever happens: a malformed reply or a timeout is an outcome, not a crash. The
    defaults are T9's; T9b passes its configuration's token cap and request timeout."""
    profile = config.DEVCASES
    cases = run_dev_cases.load_dev_cases()
    chunks, files = index.build(profile)
    endpoint = "http://127.0.0.1:%d" % PORT
    if max_tokens == 320:
        model = adapter_mod.LlamaCppAdapter(endpoint=endpoint, timeout=timeout)
    else:
        model = CappedAdapter(max_tokens, endpoint=endpoint, timeout=timeout)

    def one(question):
        started = time.time()
        row = {}
        try:
            result = answer.ask(question, profile=profile, model=model)
            row["files_used"] = result.files
            row.update(status=result.status, reason=result.reason, answer=result.answer,
                       citations=result.citations, invalid_citations=list(result.invalid),
                       model_seconds=round(result.timing.get("model_seconds", 0.0), 3))
        except adapter_mod.MalformedResponse as exc:
            partial = getattr(exc, "partial", None)
            row["files_used"] = partial.files if partial is not None else None
            row.update(status="malformed", reason=str(exc))
        except adapter_mod.ModelUnavailable as exc:
            partial = getattr(exc, "partial", None)
            row["files_used"] = partial.files if partial is not None else None
            elapsed = time.time() - started
            if "returned non-JSON" in str(exc):
                # The server answered, but not with JSON: a malformed reply, not an outage.
                row.update(status="malformed", reason=type(exc).__name__)
            elif elapsed >= timeout - 1 or "timed out" in str(exc):
                row.update(status="timeout", reason=type(exc).__name__)
            else:
                row.update(status="unavailable", reason=type(exc).__name__)
        row["total_seconds"] = round(time.time() - started, 3)
        # ask() rebuilds the corpus on every request: check it is still the manifest's.
        row["corpus_version_after"] = run_dev_cases.corpus_version(index.build(profile)[0])
        return row

    report = {"case_order": [c["id"] for c in cases], "code": code_hashes(extra_code),
              "cases_pin": cases_pin(cases),
              "corpus_version": run_dev_cases.corpus_version(chunks), "corpus_files": files,
              "request_signature": adapter_mod.request_signature(answer.PROMPT, max_tokens)}
    report["warmup_started"] = time.time()
    report["warmup"] = one(WARMUP)
    report["cases_started"] = time.time()
    rows = []
    for case in ([] if warmup_only else cases):
        row = one(case["question"])
        row["id"] = case["id"]
        row["expected_fragments"] = case.get("expected_fragments", [])
        rows.append(row)
    report["cases_ended"] = time.time()
    report["cases"] = rows
    with outpath.open_for_write(Path(report_path)) as fh:
        fh.write(json.dumps(report, indent=1, sort_keys=True))
    return 0


# --- parent: the protocol -----------------------------------------------------------------

class Run:
    # The protocol's shape; T9b overrides these four (class attributes, so T9 is unchanged).
    order = ORDER
    drift_positions = DRIFT_POSITIONS
    scored_baseline_positions = SCORED_BASELINE_POSITIONS
    baseline_key = "B0"

    def __init__(self, label, cooldown, models, evidence, manifest):
        self.label, self.cooldown, self.models, self.ev = label, cooldown, models, evidence
        self.manifest = manifest
        self.sandbox = evidence / "sandbox-nonet.sb"
        self.blocks = []          # every attempt, in order
        self.scored = {}          # position -> attempt index used for scoring

    def write(self, name, obj):
        with outpath.open_for_write(self.ev / name) as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj, indent=1, sort_keys=True))

    def sandboxed_argv(self, argv):
        return ["sandbox-exec", "-f", str(self.sandbox)] + argv

    def start_server(self, key, log):
        return subprocess.Popen(self.sandboxed_argv([str(RUNTIME), "-m", str(self.models[key]["path"])]
                                                    + SERVER_FLAGS),
                                stdout=log, stderr=subprocess.STDOUT, env=clean_env(), cwd=str(HERE))

    @staticmethod
    def wait_up(server):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             adapter_mod.RefuseRedirects())
        for _ in range(600):  # up to 5 minutes for a 5 GB model to load
            if server.poll() is not None:
                return False
            try:
                with opener.open("http://127.0.0.1:%d/health" % PORT, timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def request_signature_for(self, key):
        return self.manifest["request_signature"]

    def child_argv(self, key, report, warmup_only):
        return ([sys.executable, str(Path(__file__).resolve()), "--child", str(report)]
                + (["--warmup-only"] if warmup_only else []))

    def version_problems(self, data, warmup_only=False, key=None):
        """Code, corpus, request or cases differing from the manifest void the whole run."""
        m = self.manifest
        problems = []
        if data.get("code") != m["code"]:
            problems.append("executed code differs from the manifest")
        if data.get("request_signature") != self.request_signature_for(key):
            problems.append("request signature differs from the manifest")
        if data.get("cases_pin") != m["cases_pin"] or data.get("case_order") != m["case_order"]:
            problems.append("dev cases differ from the manifest")
        if data.get("corpus_version") != m["corpus"]["version"]:
            problems.append("corpus version differs from the manifest")
        rows = [data.get("warmup", {})] + ([] if warmup_only else data.get("cases", []))
        for row in rows:
            used = row.get("files_used")
            # A request that never reached the corpus (no partial result) has nothing to compare.
            if used is not None and used != m["corpus"]["files"]:
                problems.append("a request used a corpus that differs from the manifest")
                break
            if row.get("corpus_version_after") != m["corpus"]["version"]:
                problems.append("the corpus changed during the block")
                break
        return problems

    def check_report(self, data, warmup_only=False):
        """Measurement problems void the block (drift re-runs may replace it)."""
        m = self.manifest
        problems = []
        if warmup_only:
            warm = data.get("warmup", {})
            if warm.get("status") in (None, "unavailable", "timeout", "malformed"):
                problems.append("warm-up did not complete (%s)" % warm.get("status"))
            return problems
        cases = data.get("cases", [])
        if [c.get("id") for c in cases] != m["case_order"] or len(cases) != CASES_PER_BLOCK:
            problems.append("expected %d cases in manifest order" % CASES_PER_BLOCK)
        if any("total_seconds" not in c for c in cases):
            problems.append("a case has no elapsed time")
        if any(c.get("status") == "unavailable" for c in cases):
            problems.append("the model server stopped answering (not a timeout)")
        warm = data.get("warmup", {})
        if warm.get("status") in (None, "unavailable", "timeout"):
            problems.append("warm-up did not complete (%s)" % warm.get("status"))
        return problems

    def block(self, position, key, attempt_note="", warmup_only=False):
        n = len(self.blocks) + 1
        tag = "%02d-pos%d-%s" % (n, position, key)
        time.sleep(self.cooldown)
        record = {"attempt": n, "position": position, "model": key, "note": attempt_note,
                  "state_before": machine_state()}
        samples, failures, stop = [], [], threading.Event()   # (unix time, rss KiB); failures
        server = log = sampler = None
        data, failure = None, None

        def sample():
            while not stop.is_set() and server.poll() is None:
                try:
                    out = subprocess.run(["ps", "-o", "rss=", "-p", str(server.pid)],
                                         capture_output=True, text=True, timeout=5).stdout.strip()
                    if out.isdigit():
                        samples.append((time.time(), int(out)))
                    elif server.poll() is None:
                        failures.append((time.time(), "ps returned %r" % out[:40]))
                except Exception as exc:   # every sampling failure is evidence
                    failures.append((time.time(), type(exc).__name__))
                stop.wait(0.25)
        try:
            log = outpath.open_for_write(self.ev / ("%s-server.log" % tag))
            server = self.start_server(key, log)
            sampler = threading.Thread(target=sample, daemon=True)
            sampler.start()
            if not self.wait_up(server):
                record.update(status="server did not come up (exit %s)" % server.poll())
            else:
                report = self.ev / ("%s-dev-cases.json" % tag)
                started = time.time()
                res = subprocess.run(self.sandboxed_argv(self.child_argv(key, report, warmup_only)),
                                     env=clean_env(), cwd=str(HERE), capture_output=True, text=True)
                record.update(block_seconds=round(time.time() - started, 1), exit=res.returncode,
                              stdout_tail=res.stdout[-800:], stderr_tail=res.stderr[-800:],
                              server_alive_after=server.poll() is None)
                if res.returncode != 0 or not report.exists():
                    record.update(status="block run failed")
                else:
                    data = json.loads(report.read_text())
                    record.update(report=report.name, warmup=data.get("warmup", {}).get("status"))
                    invalid = self.version_problems(data, warmup_only, key)
                    problems = invalid + self.check_report(data, warmup_only)
                    if not record["server_alive_after"]:
                        problems.append("model server exited during the block")
                    if invalid:
                        record.update(status="run void", problems=problems)
                    elif problems:
                        record.update(status="void", problems=problems)
                    elif warmup_only:
                        record.update(status="ok", warmup_seconds=data["warmup"]["total_seconds"])
                    else:
                        lat = [c["total_seconds"] for c in data["cases"]]
                        record.update(status="ok", latencies=lat, p50=nearest_rank(lat, 0.5),
                                      case_status=[c["status"] for c in data["cases"]])
        except BaseException as exc:   # record the attempt, then propagate
            failure = exc
            record.update(status="error: %s" % type(exc).__name__)
        finally:
            stop.set()
            for step in (lambda: server and server.terminate(),
                         lambda: server and server.wait(30),
                         lambda: sampler and sampler.join(5),
                         lambda: log and log.close()):
                try:
                    step()
                except Exception as exc:
                    record.setdefault("cleanup_errors", []).append(type(exc).__name__)
                    if server is not None and server.poll() is None:
                        try:
                            server.kill()
                        except Exception:
                            pass
            try:
                self.write("%s-rss.json" % tag, {"samples": samples, "failures": failures})
            except Exception as exc:
                record.setdefault("cleanup_errors", []).append(type(exc).__name__)
            record["rss_samples"] = len(samples)
            record["rss_failures"] = len(failures)
            record["peak_rss_kib_whole_block"] = max((v for _, v in samples), default=None)
            if data is not None and not warmup_only:
                t0, t1 = data["cases_started"], data["cases_ended"]
                times = [t for t, _ in samples if t0 <= t <= t1]
                window = [v for t, v in samples if t0 <= t <= t1]
                # Scored RSS: the cases window only (the warm-up is excluded, per the brief).
                record["peak_rss_kib"] = max(window) if window else None
                # Complete measurement: no failure and no gap over 1 s inside the window,
                # and samples at both ends of it.
                edges = [t0] + times + [t1]
                gap = max((b - a for a, b in zip(edges, edges[1:])), default=None)
                record["rss_max_gap_seconds"] = round(gap, 3) if gap is not None else None
                bad = [f for f in failures if t0 <= f[0] <= t1]
                if record.get("status") == "ok" and (not window or bad or gap is None or gap > 1.0):
                    record.update(status="void", problems=["RSS not measured completely in the "
                                                           "cases window (%d failures, max gap %s s)"
                                                           % (len(bad), record["rss_max_gap_seconds"])])
            record["state_after"] = machine_state()
        i = self._done(record)
        if failure is not None:
            raise failure
        if record.get("status") == "run void":
            raise SystemExit("run void: %s" % "; ".join(record.get("problems", [])))
        return i

    def _done(self, record):
        self.blocks.append(record)
        self.write("blocks.json", self.blocks)
        print("block %(attempt)d pos %(position)d %(model)s: %(status)s p50=%(p50)s" %
              dict(record, p50=record.get("p50")), flush=True)
        return len(self.blocks) - 1

    def drifted(self, i):
        ref = self.blocks[self.scored[1]].get("p50")
        p50 = self.blocks[i].get("p50")
        if ref is None or p50 is None:
            return True
        return abs(p50 - ref) / ref > DRIFT_LIMIT

    def go(self):
        drift_affected = False
        prev_baseline = 1
        for position, key in enumerate(self.order, 1):
            self.scored[position] = self.block(position, key)
            if position in self.drift_positions and self.drifted(self.scored[position]):
                # Re-run once the blocks since the previous baseline, then this baseline.
                for p in range(prev_baseline + 1, position):
                    self.scored[p] = self.block(p, self.order[p - 1], "drift re-run")
                self.scored[position] = self.block(position, key, "drift re-run")
                if self.drifted(self.scored[position]):
                    drift_affected = True
            if key == self.baseline_key:
                prev_baseline = position
        return self.summary(drift_affected)

    def summary(self, drift_affected):
        per_model = {}
        for position, key in enumerate(self.order, 1):
            if key == self.baseline_key and position not in self.scored_baseline_positions:
                continue  # T9 position 5, T9b position 4: drift check only
            per_model.setdefault(key, []).append(self.blocks[self.scored[position]])
        out = {"label": self.label, "drift_affected": drift_affected,
               "protocol_cooldown": self.cooldown == COOLDOWN,
               "scored_attempts": {p: self.blocks[i]["attempt"] for p, i in self.scored.items()},
               "models": {}}
        for key, blocks in per_model.items():
            ok = all(b.get("status") == "ok" for b in blocks)
            lat = [x for b in blocks for x in b.get("latencies", [])] if ok else []
            rss = [b.get("peak_rss_kib") for b in blocks]
            ok = ok and all(rss)        # both scored blocks must have a measured peak
            lat = lat if ok else []
            out["models"][key] = {
                "file": self.models[key]["file"],
                "blocks": [b["attempt"] for b in blocks],
                "all_blocks_ok": ok,
                "n": len(lat),
                # Percentiles only over a complete n = 20; otherwise none, never a partial.
                "p50_seconds": round(nearest_rank(lat, 0.5), 3) if len(lat) == 20 else None,
                "p95_seconds": round(nearest_rank(lat, 0.95), 3) if len(lat) == 20 else None,
                "peak_server_rss_gib": round(max(rss) / 1048576, 2) if ok and rss else None,
            }
        self.write("summary.json", out)
        return out


def network_denied(sandbox):
    """Prove the profile denies outbound connections, without leaving the machine: a
    loopback listener on a port outside the allow-list must accept a connection made
    outside the sandbox (positive control) and refuse one made inside it with EPERM."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", PROBE_PORT))
    listener.listen(4)
    probe = ("import socket,errno,sys\ns=socket.socket()\ns.settimeout(5)\n"
             "try:\n s.connect(('127.0.0.1',%d)); print('connected')\n"
             "except OSError as e:\n print('errno', e.errno)\n" % PROBE_PORT)
    try:
        outside = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                                 env=clean_env()).stdout.strip()
        inside = subprocess.run(["sandbox-exec", "-f", str(sandbox), sys.executable, "-c", probe],
                                capture_output=True, text=True, env=clean_env())
    finally:
        listener.close()
    result = {"outside": outside, "inside": inside.stdout.strip(), "inside_exit": inside.returncode,
              "inside_stderr": inside.stderr[-300:]}
    import errno as _errno
    result["denied"] = (outside == "connected" and inside.returncode == 0
                        and inside.stdout.strip() == "errno %d" % _errno.EPERM)
    return result


def verify_checkout(commit):
    def git(*a):
        res = subprocess.run(["git", "-C", str(HERE)] + list(a), capture_output=True, text=True)
        if res.returncode != 0:
            raise SystemExit("git %s failed (%d): %s" % (" ".join(a), res.returncode,
                                                         res.stderr.strip()[-300:]))
        return res.stdout.strip()
    head = git("rev-parse", "HEAD")
    if not head or not commit or not head.startswith(commit):
        raise SystemExit("HEAD %s is not the commit given (%s)" % (head, commit))
    if git("status", "--porcelain", "--untracked-files=no"):
        raise SystemExit("tracked files are modified; run from a clean checkout of %s" % commit)
    return head


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--warmup-only", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--label")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN)
    ap.add_argument("--manifest", help="the pre-download manifest")
    ap.add_argument("--commit", help="dbrain-runtime commit the run uses (checked against HEAD)")
    ap.add_argument("--dry-check", action="store_true",
                    help="pins, network probe, and each model's load and warm-up only; no dev case is run")
    args = ap.parse_args(argv)
    if args.child:
        return child(args.child, args.warmup_only)
    if not (args.label and args.manifest and args.commit):
        ap.error("--label, --manifest and --commit are required")
    if args.cooldown != COOLDOWN and not args.dry_check:
        raise SystemExit("the protocol's cool-down is %d s; other values are refused" % COOLDOWN)
    if not LABEL.match(args.label):
        raise SystemExit("label must be letters, digits, '.', '_' or '-' only")

    head = verify_checkout(args.commit)
    pre = json.loads(Path(args.manifest).read_text())["candidates"]
    models = {"B0": dict(BASELINE, path=APP / "models" / BASELINE["file"])}
    for key in ("C1", "C2", "C3"):
        c = pre[key]
        models[key] = {"file": c["file"], "sha256": c["sha256"], "size": c["size"],
                       "repo": c["repo"], "revision": c["revision"],
                       "path": APP / "models" / c["file"]}
    pins = {"runtime": {"path": str(RUNTIME), "sha256": sha256(RUNTIME)}}
    if pins["runtime"]["sha256"] != RUNTIME_PIN:
        raise SystemExit("runtime does not match its pin")
    for key, m in models.items():
        got = sha256(m["path"])
        if got != m["sha256"]:
            raise SystemExit("%s does not match its pin: %s" % (key, got))
        pins[key] = {k: (str(v) if isinstance(v, Path) else v) for k, v in m.items()}

    # Evidence only under this repository's artifacts/ folder (the brief's output boundary).
    root = (HERE / "artifacts").resolve()
    evidence = root / "t9" / ("%s-%s" % ("dry" if args.dry_check else "run", args.label))
    evidence = Path(outpath.make_dir(evidence)).resolve()
    if root not in evidence.parents:
        raise SystemExit("evidence folder escaped artifacts/: %s" % evidence)

    profile = config.DEVCASES
    chunks, files = index.build(profile)
    cases = run_dev_cases.load_dev_cases()
    if len(cases) != CASES_PER_BLOCK:
        raise SystemExit("expected %d dev cases, found %d" % (CASES_PER_BLOCK, len(cases)))
    manifest = {
        "brief": "90 System/briefs/T9-M6-model-comparison.md",
        "runtime_commit": head,
        "runner_sha256": sha256(Path(__file__)),
        "code": code_hashes(),
        "corpus": {"profile": profile.name, "files": files,
                   "version": run_dev_cases.corpus_version(chunks)},
        "request_signature": adapter_mod.request_signature(answer.PROMPT),
        "case_order": [c["id"] for c in cases],
        "cases_pin": cases_pin(cases),
        "warmup_question": WARMUP,
        "server_flags": SERVER_FLAGS,
        "server_sandboxed": True,
        "block_order": ORDER,
        "cooldown_seconds": args.cooldown,
        "protocol_cooldown": args.cooldown == COOLDOWN,
        "case_timeout_seconds": CASE_TIMEOUT,
        "drift_limit": DRIFT_LIMIT,
        "pins": pins,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    run = Run(args.label, args.cooldown, models, evidence, manifest)
    run.write("sandbox-nonet.sb", SANDBOX)
    net = network_denied(run.sandbox)
    manifest["network_check"] = net
    if not net["denied"]:
        run.write("manifest-pre-run.json", manifest)
        raise SystemExit("could not prove the sandbox denies outbound connections; stopping: %s" % net)
    run.write("manifest-pre-run.json", manifest)

    if args.dry_check:
        run.cooldown = 0
        # Every model loads on the pinned build and answers the warm-up; no dev case runs.
        done = {key: run.blocks[run.block(0, key, "dry check: warm-up only, no dev case run",
                                          warmup_only=True)] for key in ("B0", "C1", "C2", "C3")}
        print(json.dumps({key: {k: b.get(k) for k in ("status", "problems", "warmup",
                                                     "warmup_seconds", "peak_rss_kib_whole_block")}
                          for key, b in done.items()}, indent=1))
        return 0 if all(b.get("status") == "ok" for b in done.values()) else 1
    print(json.dumps(run.go(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
