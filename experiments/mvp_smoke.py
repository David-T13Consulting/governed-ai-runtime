#!/usr/bin/env python3
"""MVP-phone end-to-end smoke test on loopback, with the real model and the whole vault.

Starts the pinned llama-server from ~/Library/Application Support/dbrain, serves /ask on
127.0.0.1 with a throwaway token that exists only in this process's memory, asks five
questions plus the refusal checks, stops everything and writes a token-free summary to
artifacts/ through the write guard. Run under the no-network sandbox profile.
"""
import http.client
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from dbrain import adapter, outpath, serve  # noqa: E402

APP = Path.home() / "Library/Application Support/dbrain"
SERVER = APP / "llama/llama-b11146/llama-server"
MODEL = APP / "models/gemma-4-E4B_q4_0-it.gguf"  # the default since Q84 (a)
PORT = 18765  # fixed, so the no-network sandbox profile can allow exactly this one
# Its own model port, not the live service's 18087: with the login item running, a smoke
# run on 18087 would fail to start its server and silently ask the live one instead.
LLAMA_PORT = 18088
LLAMA = "http://127.0.0.1:%d" % LLAMA_PORT
# (question, any of these substrings expected in the answer, case-insensitive; None = should decline)
QUESTIONS = [
    ("What counts as a tempo session?", ["marathon pace"]),
    ("How should an easy run feel?", ["conversational"]),
    ("What does the data policy say about client notes?", ["never", "client"]),
    ("How long are failed answers kept?", ["90 days"]),
    ("What is the owner's bank account number?", None),
    # Synthetic notes in examples/sample-vault, against the fixed SMOKE_TODAY below.
    ("Is there choir rehearsal on Saturday 17 October?", ["no", "closed"]),
    ("What time is choir rehearsal on Saturday 24 October?", ["10:00"]),
    ("When is the book swap on 11 November?", ["18:00"]),
    ("Is there pottery class on Wednesday?", ["19:00"]),
    ("Is there choir rehearsal tomorrow?", ["no", "closed"]),
]
SMOKE_TODAY = __import__("datetime").date(2026, 10, 16)  # reproducible: relative dates resolve against this


def call(port, method, path, body=None, bearer=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=150)
    headers = {"Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = "Bearer " + bearer
    conn.request(method, path, body=json.dumps(body).encode() if body else None, headers=headers)
    r = conn.getresponse()
    out = r.status, json.loads(r.read().decode() or "{}")
    conn.close()
    return out


def main(out_dir):
    out_dir = outpath.make_dir(out_dir)
    log = outpath.open_for_write(out_dir / "llama-server.log")
    llama = subprocess.Popen([str(SERVER), "-m", str(MODEL), "--host", "127.0.0.1", "--port", str(LLAMA_PORT),
                              "--offline", "--no-webui", "-np", "1", "-c", "4096", "-t", "4",
                              "-ngl", "99", "--no-cache-prompt", "--reasoning", "off"], stdout=log, stderr=subprocess.STDOUT)
    bearer = secrets.token_urlsafe(32)
    svc = serve.Service(bearer, model_factory=lambda: adapter.LlamaCppAdapter(endpoint=LLAMA, timeout=110),
                        model_health=lambda: serve.llama_health(LLAMA), today=lambda: SMOKE_TODAY)
    httpd = serve.build_server("127.0.0.1", PORT, svc)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    summary = {"checks": {}, "questions": []}
    try:
        for _ in range(120):
            if serve.llama_health(LLAMA) == "up":
                break
            time.sleep(1)
        summary["checks"]["health"] = call(port, "GET", "/health")
        summary["checks"]["wrong_token"] = call(port, "POST", "/ask", {"question": "x"}, "nope" * 10)[0]
        summary["checks"]["no_token"] = call(port, "POST", "/ask", {"question": "x"})[0]
        for q, expect in QUESTIONS:
            code, body = call(port, "POST", "/ask", {"question": q}, bearer)
            text = (body.get("answer") or "").lower()
            if expect is None:
                hit = body.get("status") == "withheld" or "not" in text
            else:
                hit = any(e.lower() in text for e in expect)
            inbox = [c for c in body.get("citations") or [] if c["folder"] == "00 Inbox"]
            summary["questions"].append({
                "question": q, "expected_any": expect, "contains_expected": hit,
                "http": code, "status": body.get("status"),
                "latency_s": body.get("latency_s"), "answer": body.get("answer"),
                "citations": body.get("citations"), "reason": body.get("reason"),
                "inbox_citations_marked_unreviewed": all(c["unreviewed"] for c in inbox),
            })
            print("%3d %-11s %6.1f s  %d cites  %-5s %s" % (
                code, body.get("status"), body.get("latency_s", 0),
                len(body.get("citations") or []), "OK" if hit else "MISS", q), flush=True)
    finally:
        llama.terminate()
        llama.wait(timeout=20)
        log.close()
    # Model down: must be 503 "unavailable", never an answer from anywhere else.
    code, body = call(port, "POST", "/ask", {"question": QUESTIONS[0][0]}, bearer)
    summary["checks"]["model_down"] = [code, body.get("status")]
    summary["checks"]["health_model_down"] = call(port, "GET", "/health")
    httpd.shutdown()
    lat = sorted(q["latency_s"] for q in summary["questions"] if q["latency_s"] is not None)
    import run_dev_cases
    summary["latency"] = {"n": len(lat), "p50": run_dev_cases.percentile(lat, 50),
                          "p95": run_dev_cases.percentile(lat, 95), "max": lat[-1] if lat else None}
    summary["contains_expected"] = "%d/%d" % (sum(q["contains_expected"] for q in summary["questions"]),
                                              len(summary["questions"]))
    text = json.dumps(summary, indent=1, ensure_ascii=False)
    assert bearer not in text
    with outpath.open_for_write(out_dir / "smoke.json") as fh:
        fh.write(text)
    print(json.dumps(summary["checks"]), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
