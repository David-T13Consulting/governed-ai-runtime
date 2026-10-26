#!/usr/bin/env python3
"""Latency sweep over the 10 dev cases (P2.2 follow-up, 5 Oct, authorised by David).

Varies one thing at a time against run B's configuration: the output token cap, the
number of retrieved chunks (prompt size), and llama.cpp server settings. A baseline is
repeated first, in the middle and last, so drift from the machine (the Air has no fan)
can be told apart from the effect of a setting.

Production code is not changed: the cap and the chunk count are passed in from here.
The reserved acceptance cases are never loaded (run_dev_cases.load_dev_cases refuses
them). Every output goes through the write guard, into dbrain-runtime/artifacts/.

Run the whole script under the no-network sandbox profile (see run.sh next to it).
"""
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dbrain import adapter as adapter_mod  # noqa: E402
from dbrain import answer, config, outpath  # noqa: E402
import run_dev_cases  # noqa: E402
sys.path.insert(0, str(HERE))
import split_server_log  # noqa: E402

PORT = 18087
# Set by run.sh, which checks its pin first (Q42: the /private/tmp copy is gone).
MODEL = os.environ.get("DBRAIN_MODEL", str(Path.home() / "Library/Application Support/dbrain/models/Qwen3-4B-Q4_K_M.gguf"))
BASE_FLAGS = ["-c", "4096", "-t", "4", "-ngl", "99"]

# (name, server flags, max_tokens, retrieved chunks, case filter)
CONFIGS = [
    ("base-1", BASE_FLAGS, 320, 4, None),
    ("cap-160", BASE_FLAGS, 160, 4, None),
    ("cap-640", BASE_FLAGS, 640, 4, None),
    ("chunks-2", BASE_FLAGS, 320, 2, None),
    ("chunks-3", BASE_FLAGS, 320, 3, None),
    ("base-2", BASE_FLAGS, 320, 4, None),
    ("flash-attn", BASE_FLAGS + ["-fa", "on"], 320, 4, None),
    ("ubatch-1024", BASE_FLAGS + ["-b", "2048", "-ub", "1024"], 320, 4, None),
    ("threads-8", ["-c", "4096", "-t", "8", "-ngl", "99"], 320, 4, None),
    # CPU only, three cases: only to show whether the GPU is doing the work at all.
    ("cpu-only", ["-c", "4096", "-t", "4", "-ngl", "0"], 320, 4,
     ("local_stage", "client_policy", "unknown_password")),
    ("base-3", BASE_FLAGS, 320, 4, None),
]


class CappedAdapter(adapter_mod.LlamaCppAdapter):
    def __init__(self, cap, **kw):
        super().__init__(**kw)
        self.cap = cap

    def generate(self, prompt, max_tokens=None):
        return super().generate(prompt, max_tokens=self.cap)


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception as exc:  # recorded, not fatal
        return "unavailable: %s" % exc


def machine_state():
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pmset_therm": sh(["pmset", "-g", "therm"]),
        "load_avg": sh(["sysctl", "-n", "vm.loadavg"]),
        "memory_pressure": sh(["memory_pressure", "-Q"]),
        "top_cpu": sh(["ps", "-Aceo", "pcpu,comm", "-r"]).splitlines()[:6],
    }


def wait_health(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 adapter_mod.RefuseRedirects())
            with opener.open("http://127.0.0.1:%d/health" % PORT, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


def write(path, text):
    with outpath.open_for_write(path) as fh:
        fh.write(text)


def run_config(server_bin, out, name, flags, cap, chunks, only):
    d = out / name
    log_path = d / "llama-server.log"
    outpath.make_dir(d)
    launch = [server_bin, "-m", MODEL, "--host", "127.0.0.1", "--port", str(PORT),
              "--offline", "--no-webui", "-np", "1", "--no-cache-prompt"] + flags
    write(d / "launch.txt", " ".join(launch) + "\n")
    state_before = machine_state()
    with outpath.open_for_write(log_path) as log:
        proc = subprocess.Popen(launch, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_health():
            raise SystemExit("server did not come up for %s" % name)
        model = CappedAdapter(cap)
        cases = run_dev_cases.load_dev_cases()
        if only:
            cases = [c for c in cases if c["id"] in only]
        rows = []
        for case in cases:
            started = time.time()
            try:
                r = answer.ask(case["question"], profile=config.DEVCASES, model=model,
                               limit=chunks)
                row = {"status": r.status, "reason": r.reason, "answer": r.answer,
                       "model_seconds": round(r.timing.get("model_seconds", 0.0), 3)}
            except adapter_mod.MalformedResponse as exc:
                row = {"status": "malformed", "reason": str(exc)}
            row.update({"id": case["id"], "total_seconds": round(time.time() - started, 3)})
            rows.append(row)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    split = split_server_log.main(log_path, d / "server-split.csv")
    lat = [r["total_seconds"] for r in rows]
    summary = {
        "config": name, "server_flags": flags, "max_tokens": cap, "chunks": chunks,
        "cases": len(rows), "case_filter": list(only) if only else None,
        "p50_seconds": run_dev_cases.percentile(lat, 50),
        "p95_seconds": run_dev_cases.percentile(lat, 95),
        "median_prompt_tok_per_s": _median([s["prompt_tok_per_s"] for s in split]),
        "median_gen_tok_per_s": _median([s["gen_tok_per_s"] for s in split if s["gen_tok_per_s"]]),
        "median_prompt_tokens": _median([s["prompt_tokens"] for s in split]),
        "prompt_share_of_server_time": round(sum(s["prompt_ms"] for s in split)
                                             / sum(s["total_ms"] for s in split), 3),
        "malformed": [r["id"] for r in rows if r["status"] == "malformed"],
        "machine_before": state_before,
        "machine_after": machine_state(),
        "rows": rows,
    }
    write(d / "result.json", json.dumps(summary, indent=1, sort_keys=True))
    print("%-12s p50 %5.1f s  p95 %5.1f s  prompt %5.1f t/s  gen %4.1f t/s  prompt tok %4s  malformed %s"
          % (name, summary["p50_seconds"], summary["p95_seconds"],
             summary["median_prompt_tok_per_s"], summary["median_gen_tok_per_s"],
             summary["median_prompt_tokens"], summary["malformed"]), flush=True)
    return summary


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def main(server_bin, out):
    out = Path(out)
    write(out / "host.json", json.dumps({"platform": platform.platform(),
                                         "python": platform.python_version(),
                                         "model": MODEL,
                                         "prompt_version": adapter_mod.request_signature(answer.PROMPT),
                                         "start": machine_state()}, indent=1))
    results = []
    for name, flags, cap, chunks, only in CONFIGS:
        try:
            results.append(run_config(server_bin, out, name, flags, cap, chunks, only))
        except (SystemExit, Exception) as exc:  # one bad flag must not lose the sweep
            print("%-12s FAILED: %s" % (name, exc), flush=True)
            results.append({"config": name, "failed": str(exc)})
        time.sleep(30)  # same short pause between every config; drift shows in the baselines
    write(out / "summary.json", json.dumps(
        [{k: v for k, v in r.items() if k not in ("rows", "machine_before", "machine_after")}
         for r in results], indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
