#!/usr/bin/env python3
"""Split a llama-server log into per-request prompt-reading and generation times.

The server prints three timing lines per request (prompt eval, eval, total). This pairs
them by task id and writes a CSV next to nothing in the vault: the output path goes
through the same write guard as every other output.
"""
import csv
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dbrain import outpath  # noqa: E402

LINE = re.compile(
    r"^(?P<ts>\S+) I slot print_timing: id\s+\d+ \| task (?P<task>\d+) \|\s+"
    r"(?P<kind>prompt eval time|eval time|total time) =\s+(?P<ms>[\d.]+) ms /\s+(?P<tok>\d+) tokens"
)


def parse(text):
    rows = {}
    order = []
    for line in text.splitlines():
        m = LINE.match(line)
        if not m:
            continue
        t = m["task"]
        if t not in rows:
            rows[t] = {"task": int(t), "ts": m["ts"]}
            order.append(t)
        key = {"prompt eval time": "prompt", "eval time": "gen", "total time": "total"}[m["kind"]]
        rows[t][key + "_ms"] = float(m["ms"])
        rows[t][key + "_tokens"] = int(m["tok"])
    out = []
    for i, t in enumerate(order, 1):
        r = rows[t]
        r["request"] = i
        r["prompt_tok_per_s"] = round(r["prompt_tokens"] / (r["prompt_ms"] / 1000), 1)
        r["gen_tok_per_s"] = round(r["gen_tokens"] / (r["gen_ms"] / 1000), 1) if r.get("gen_ms") else None
        r["prompt_share"] = round(r["prompt_ms"] / r["total_ms"], 3)
        out.append(r)
    return out


def main(log, dest):
    rows = parse(Path(log).read_text())
    cols = ["request", "task", "ts", "prompt_tokens", "prompt_ms", "prompt_tok_per_s",
            "gen_tokens", "gen_ms", "gen_tok_per_s", "total_ms", "prompt_share"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    with outpath.open_for_write(dest) as fh:
        fh.write(buf.getvalue())
    return rows


if __name__ == "__main__":
    for r in main(sys.argv[1], sys.argv[2]):
        print("%2d  prompt %4d tok %7.0f ms %6.1f t/s | gen %3d tok %7.0f ms %5.1f t/s | prompt share %.2f"
              % (r["request"], r["prompt_tokens"], r["prompt_ms"], r["prompt_tok_per_s"],
                 r["gen_tokens"], r["gen_ms"], r["gen_tok_per_s"], r["prompt_share"]))
