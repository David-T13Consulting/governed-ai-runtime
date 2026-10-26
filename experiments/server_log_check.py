#!/usr/bin/env python3
"""P2.7 / data policy §7b: does the model server's log hold question or answer text?

Starts the pinned llama-server with the default model (C2) and the service's flags, writing
to a fresh, empty log file in a temporary folder outside git and the vault, asks one planted
question through the service's adapter, stops the server, and reports `contains_question`
and `contains_answer` as yes/no, plus the checks that make them count, never log excerpts,
on success or failure. Run under the no-network sandbox profile:

    sandbox-exec -f experiments/sandbox-smoke.sb python3 experiments/server_log_check.py

The question carries one unique marker and asks the model to reply with a second unique
marker, so both texts are known and distinctive. The check only counts if the port was free
before the start, the server it started is the one that answered (still running, and its own
log records the request's timings), the reply contains the answer marker, and the flags are
the launcher's (mvp/run-all.sh), so the log is the one the service would write.

If the log does contain the question or the answer, that log falls under §7a (local, 90
days, the local model and David only) instead of §7b.
"""
import json
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from dbrain import adapter, outpath  # noqa: E402

APP = Path.home() / "Library/Application Support/dbrain"
SERVER = APP / "llama/llama-b11146/llama-server"
MODEL = APP / "models/gemma-4-E4B_q4_0-it.gguf"
PORT = 18088   # allowed by experiments/sandbox-smoke.sb; not the live service's 18087
FLAGS = ["--host", "127.0.0.1", "--port", str(PORT), "--offline", "--no-webui", "-np", "1",
         "-c", "4096", "-t", "4", "-ngl", "99", "--no-cache-prompt", "--reasoning", "off"]
TIMING_LINE = "prompt eval time"   # llama-server logs this for every completed request
LAUNCHER = HERE / "mvp" / "run-all.sh"


def flags_match_launcher():
    """The launcher starts the server with the same flags, its own port apart."""
    text = LAUNCHER.read_text(encoding="utf-8")
    start = text.index('"$SERVER" -m "$MODEL"')
    words = text[start:text.index(">", start)].replace("\\\n", " ").split()[3:]
    i = words.index("--port")
    words[i + 1] = str(PORT)
    return words == FLAGS


def yes_no(flag):
    return None if flag is None else ("yes" if flag else "no")


def port_free(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def main():
    """Print one JSON line of yes/no results and checks; exit 0 only if every check holds
    and nothing went wrong. Any error, wherever it happens (preflight, server, request,
    log close, cleanup, reading the log), is reported by its type only, never its message
    (it could quote the log), and fails the check."""
    result = {"port_was_free": None, "flags_match_launcher": None, "server_started": False,
              "own_server_answered": False, "reply_has_answer_marker": False,
              "contains_question": None, "contains_answer": None}
    try:
        _check(result)
    except Exception as exc:
        result.setdefault("error", type(exc).__name__)
    print(json.dumps(result, sort_keys=True))
    ok = ("error" not in result and result["port_was_free"] and result["flags_match_launcher"]
          and result["own_server_answered"] and result["reply_has_answer_marker"]
          and result["contains_question"] == "no" and result["contains_answer"] == "no")
    return 0 if ok else 1


def _check(result):
    q_marker = "plantedq" + secrets.token_hex(5)
    a_marker = "planteda" + secrets.token_hex(5)
    question = ("Reference %s. Reply with exactly this one word and nothing else: %s"
                % (q_marker, a_marker))
    result["port_was_free"] = port_free(PORT)
    result["flags_match_launcher"] = flags_match_launcher()
    if not (result["port_was_free"] and result["flags_match_launcher"]):
        return
    workdir = Path(tempfile.mkdtemp(prefix="dbrain-server-log-check-"))
    outpath.assert_outside_git(workdir)
    log_path = workdir / "llama-server.log"
    server = None
    try:
        with outpath.open_for_write(log_path) as log:   # fresh and empty
            server = subprocess.Popen([str(SERVER), "-m", str(MODEL)] + FLAGS,
                                      stdout=log, stderr=subprocess.STDOUT)
            for _ in range(240):
                if server.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/health" % PORT, timeout=2) as r:
                        if r.status == 200:
                            result["server_started"] = True
                            break
                except Exception:
                    time.sleep(0.5)
            if result["server_started"] and server.poll() is None:
                model = adapter.LlamaCppAdapter(endpoint="http://127.0.0.1:%d" % PORT, timeout=120)
                reply, _ = model.generate(question, max_tokens=32)
                result["reply_has_answer_marker"] = a_marker in reply
                result["own_server_answered"] = server.poll() is None
    except Exception as exc:
        result["error"] = type(exc).__name__
    finally:
        if server is not None:
            # Reaped before the log is read. A server that will not stop in time fails the
            # check (by error type), is killed, and is waited for; if even that fails, the
            # exception reaches main(), which reports it and fails.
            server.terminate()
            try:
                server.wait(20)
            except Exception as exc:
                result.setdefault("error", type(exc).__name__)
                server.kill()
                server.wait(20)
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    # The server we started wrote this request's timings to this log: it is the one that answered.
    result["own_server_answered"] = result["own_server_answered"] and TIMING_LINE in text
    result["contains_question"] = yes_no(q_marker in text)
    result["contains_answer"] = yes_no(a_marker in text)
    result["log_bytes"] = len(text.encode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
