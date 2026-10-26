#!/usr/bin/env python3
"""Run the 10 existing development retrieval cases (P2.2 "Done when" 5).

Refuses to touch the 6 reserved acceptance cases: they are held back for P2.1's
independent review, and running a model over them would burn the held-out set.
"""
import argparse
import hashlib
import json
import math
import os
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

from dbrain import adapter as adapter_mod
from dbrain import answer, config, index, outpath, retrieve

BENCH = Path(__file__).resolve().parent / "examples"
RESERVED = "acceptance-reserved.json"


def load_dev_cases(path=None):
    p = Path(path or (BENCH / "dev-cases.json"))
    if p.name == RESERVED or "reserved" in p.name:
        raise SystemExit("refusing to load the reserved acceptance cases: %s" % p)
    cases = json.loads(p.read_text())
    if len(cases) != 10:
        print("warning: expected 10 dev cases, found %d" % len(cases), file=sys.stderr)
    return cases


def percentile(values, q):
    """Nearest-rank percentile: the smallest value with at least q% of values at or below
    it, so rank = ceil(q/100 * n). `round` was not nearest-rank (round D finding 8)."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(math.ceil(q / 100.0 * len(ordered))))
    return ordered[min(k, len(ordered)) - 1]


def corpus_version(chunks):
    """Hash of exactly what was indexed: every chunk's citation and text, in order."""
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.cite().encode("utf-8") + b"\0" + c.text.encode("utf-8") + b"\0")
    return h.hexdigest()[:12]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="devcases", choices=sorted(config.PROFILES))
    ap.add_argument("--stub-model", action="store_true",
                    help="measure retrieval only; no weights needed")
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    profile = config.PROFILES[args.profile]
    cases = load_dev_cases()
    index_started = time.time()
    chunks, files = index.build(profile)
    ix = retrieve.Index(chunks)
    index_seconds = time.time() - index_started

    model = (adapter_mod.StubAdapter() if args.stub_model
             else adapter_mod.LlamaCppAdapter(endpoint=args.endpoint))

    rows = []
    latencies = []
    for case in cases:
        started = time.time()
        hits = ix.search(case["question"])
        retrieval_s = time.time() - started

        evidence = "\n".join(c.text for _, c in hits)
        fragments = case.get("expected_fragments", [])
        found = [f for f in fragments if f in evidence]

        # Two of the ten cases expect an abstention (no expected_fragments): the right
        # outcome there is to retrieve nothing usable, not to find a fragment.
        kind = "grounded" if fragments else "abstain"
        row = {
            "id": case["id"],
            "kind": kind,
            "retrieved": [c.cite() for _, c in hits],
            "retrieval_seconds": round(retrieval_s, 4),
            "expected_fragments": fragments,
            "fragments_found_in_evidence": found,
        }
        if kind == "grounded":
            row["retrieval_hit"] = len(found) == len(fragments)
        else:
            # Retrieval "passes" an abstention case only by returning nothing at all.
            row["retrieval_abstained"] = not hits

        if not args.stub_model:
            # End-to-end latency is the wall clock of one ask(), which indexes, retrieves
            # and generates itself. Adding this loop's own index/retrieval timings to the
            # model time from ask() mixed two executions (round C finding 6).
            ask_started = time.time()
            try:
                result = answer.ask(case["question"], profile=profile, model=model)
            except adapter_mod.MalformedResponse as exc:
                # A real outcome of the model, not an outage: record it, keep measuring.
                result = None
                row.update({"status": "malformed", "reason": str(exc)})
            except adapter_mod.ModelUnavailable as exc:
                raise SystemExit("MODEL UNAVAILABLE: %s" % exc)
            total = time.time() - ask_started
            latencies.append(total)
            row["total_seconds"] = round(total, 3)
            if result is not None:
                row.update({
                    "status": result.status,
                    "reason": result.reason,
                    "grounding_overlap": (None if result.grounding is None
                                          else round(result.grounding, 3)),
                    "answer": result.answer,
                    "model_seconds": round(result.timing.get("model_seconds", 0.0), 3),
                    "citations": result.citations,
                    "invalid_citations": list(result.invalid),
                    "answer_contains_expected": [
                        f for f in fragments if f.lower() in result.answer.lower()
                    ],
                })
        rows.append(row)

    grounded = [r for r in rows if r["kind"] == "grounded"]
    abstain = [r for r in rows if r["kind"] == "abstain"]
    recall = sum(1 for r in grounded if r["retrieval_hit"])
    abstained = sum(1 for r in abstain if r["retrieval_abstained"])
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": profile.name,
        "case_count": len(rows),
        "reserved_cases_run": 0,
        "corpus": {"files": files, "chunk_count": len(chunks),
                   "version": corpus_version(chunks)},
        "retrieval_recall_grounded_cases": "%d/%d" % (recall, len(grounded)),
        "retrieval_abstained_on_abstention_cases": "%d/%d" % (abstained, len(abstain)),
        "abstention_note": (
            "Retrieval returns near-miss evidence for both questions the corpus cannot "
            "answer, so abstention falls entirely to the model rather than to retrieval. "
            "Marked `defer`, not `pass`."
        ),
        "model": model.describe(),
        "index_seconds_once": round(index_seconds, 3),
        "prompt_version": adapter_mod.request_signature(answer.PROMPT),
        "prompt_version_covers": "chat path, system turn, user template, sampling params, max_tokens",
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "peak_rss_bytes_this_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "peak_rss_caveat": (
                "This is the Python client only. It excludes the model server process, "
                "which is where the memory that matters for the 24-vs-32 GB decision "
                "lives. Not a memory ceiling for T9."
            ),
        },
        "latency": None,
        "cases": rows,
    }
    if latencies:
        report["latency"] = {
            "n": len(latencies),
            "p50_seconds": round(percentile(latencies, 50), 3),
            "p95_seconds": round(percentile(latencies, 95), 3),
            "method": "nearest-rank; n is small, so p95 is near the maximum",
        }
    else:
        report["latency_note"] = (
            "No model was run, so no end-to-end latency is reported. Retrieval timings "
            "only. p50/p95 against the D56 targets need the local model."
        )

    out = Path(args.out or (config.ARTIFACTS / ("dev-cases-%s.json" % time.strftime("%Y-%m-%d"))))
    try:
        outpath.assert_outside_vault(out)   # --out must never truncate a vault note
    except outpath.OutsideVaultError as exc:
        raise SystemExit("REFUSED: %s" % exc)
    # No mkdir: open_for_write creates missing directories inside its anchored walk.
    with outpath.open_for_write(out) as fh:
        fh.write(json.dumps(report, indent=1, sort_keys=True))
    print("grounded-case retrieval recall: %d/%d   retrieval abstained on %d/%d abstention cases   reserved run: 0"
          % (recall, len(grounded), abstained, len(abstain)))
    print("report: %s" % out)
    for r in rows:
        if r["kind"] == "grounded":
            mark = "HIT " if r["retrieval_hit"] else "miss"
        else:
            mark = "ABST" if r["retrieval_abstained"] else "defer"
        print("  %-18s %-8s %s  %s" % (r["id"], r["kind"], mark,
                                       r["retrieved"][0] if r["retrieved"] else "(nothing retrieved)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
