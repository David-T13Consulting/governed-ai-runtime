"""Redacted per-query trace, written outside the vault and outside git (P2.2 "Done when"
6; data policy §7b; P2.7 step C3b).

The question is hashed, not stored: traces must not become a shadow copy of what David
asks. The destination is checked by outpath.py, because it is configurable and a
symlinked directory could otherwise aim a write back into the vault or into a repository.
"""
import hashlib
import json
import time
from pathlib import Path

from . import adapter as adapter_mod
from . import config, outpath


def write(result, model, profile, artifacts=None, answer_id=None):
    root = Path(artifacts or config.LOGS)
    outpath.assert_outside_vault(root)
    outpath.assert_outside_git(root)
    # No mkdir here: open_for_write creates missing directories inside its anchored walk.
    path = root / ("traces-%s.jsonl" % time.strftime("%Y-%m-%d"))
    outpath.assert_outside_vault(path)

    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "question_sha256": hashlib.sha256(result.question.encode("utf-8")).hexdigest(),
        "question_chars": len(result.question),
        "profile": profile.name,
        "status": result.status,
        "reason": result.reason,
        # A fixed code (P2.7 step C1b/C2, D113): lets the trace count unverified answers
        # apart from defects without any question or answer text.
        "reason_code": getattr(result, "reason_code", None),
        # The random id the phone got with this answer (P2.7 step C5): ties a "wrong" tap
        # in the audit log to this line. Not derived from the question; None outside /ask.
        "answer_id": answer_id,
        "grounding_overlap": result.grounding,
        # Everything the query actually opened. Recording only the retrieved hits
        # understated this: indexing reads every allowed note, including on a withheld
        # query that retrieved nothing.
        "files_indexed": sorted(result.files),
        "files_indexed_count": len(result.files),
        # Read, but refused by the label rules and never indexed (whole-vault profile).
        "files_read_but_skipped": sorted(p for p, _ in getattr(result, "skipped", [])),
        "paths_retrieved": sorted({c.path for _, c in result.hits}),
        "citations": list(result.citations),
        "invalid_citations": list(result.invalid),
        "timing": result.timing,
        "model": model.describe(),
        "cloud_keys_present_but_unused": adapter_mod.cloud_keys_present(),
    }
    with outpath.open_for_write(path, append=True, outside_git=True) as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return path
