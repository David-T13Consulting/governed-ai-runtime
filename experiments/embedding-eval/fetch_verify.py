#!/usr/bin/env python3
"""M6b S2: fetch the two embedding models exactly as pinned in manifest.json, and verify.

    "$HOME/Library/Application Support/dbrain/venvs/m6b-py312/bin/python" \
        experiments/embedding-eval/fetch_verify.py [--verify-only]

Only the files the manifest lists, at its immutable revision, into
~/Library/Application Support/dbrain/models/embeddings/<org>__<name>@<rev12>/ (outside the
vault, iCloud and git; the Hugging Face cache is pointed there too, so no copy lands in
the home cache or the vault). Every file is checked against the manifest's size and full
SHA-256. A revision that is no longer the publisher's current one, a missing file, or any
mismatch stops with exit 1 (protocol item 5: a change is a new candidate). The
verification record goes next to this script: verification.json.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"
ROOT = Path.home() / "Library" / "Application Support" / "dbrain" / "models" / "embeddings"
# Every Hugging Face cache location is forced into the app folder, whatever the caller's
# environment says (Codex review, S2 round 1), so no copy can land in the vault or the home
# cache.
for _var, _sub in (("HF_HOME", ".hf-home"), ("HF_HUB_CACHE", ".hf-home/hub"),
                   ("HF_XET_CACHE", ".hf-home/xet"), ("HF_ASSETS_CACHE", ".hf-home/assets")):
    os.environ[_var] = str(ROOT / _sub)
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


class Stop(Exception):
    """The S2 stop rule: a newer revision or any mismatch ends the step at once."""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def target(model):
    org, name = model["repository"].split("/")
    return ROOT / ("%s__%s@%s" % (org, name, model["revision"][:12]))


def _check(dest, f):
    p = dest / f["path"]
    got = {"path": f["path"], "exists": p.is_file()}
    if got["exists"]:
        got["size"] = p.stat().st_size
        got["sha256"] = sha256(p)
    got["ok"] = got["exists"] and got["size"] == f["size"] and got["sha256"] == f["sha256"]
    return got


def _fetch_model(model, record, verify_only):
    """Fetch (unless verify_only) and verify one model, file by file; raise Stop at the
    first problem, after recording it, so nothing more is downloaded."""
    dest = target(model)
    entry = {"repository": model["repository"], "revision": model["revision"],
             "folder": str(dest), "files": [], "ok": True}
    record["models"].append(entry)
    if not verify_only:
        from huggingface_hub import HfApi, hf_hub_download
        current = HfApi().model_info(model["repository"]).sha
        entry["publisher_revision_at_fetch"] = current
        if current != model["revision"]:
            entry["ok"] = False
            raise Stop("%s: the publisher's revision changed (a new candidate)"
                       % model["repository"])
    for f in model["files"]:
        if not verify_only:
            hf_hub_download(model["repository"], f["path"], revision=model["revision"],
                            local_dir=str(dest))
        got = _check(dest, f)
        entry["files"].append(got)
        entry["bytes"] = sum(g.get("size", 0) for g in entry["files"])
        if not got["ok"]:
            entry["ok"] = False
            raise Stop("%s: %s does not match the manifest" % (model["repository"], f["path"]))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args(argv)
    manifest = json.loads(MANIFEST.read_text())
    if not str(ROOT).startswith(str(Path.home() / "Library" / "Application Support" / "dbrain")):
        raise SystemExit("refusing: target outside the app folder")
    record = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "models": [], "ok": True}
    try:
        for model in manifest["models"]:
            _fetch_model(model, record, args.verify_only)
    except Stop as exc:
        record["ok"] = False
        record["stopped"] = str(exc)
    record["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (HERE / "verification.json").write_text(json.dumps(record, indent=1) + "\n")
    for e in record["models"]:
        bad = [g["path"] for g in e["files"] if not g["ok"]]
        print("%-32s %s  %d files, %d bytes%s" % (e["repository"], "OK" if e["ok"] else "FAIL",
                                                  len(e["files"]), e.get("bytes", 0),
                                                  ("  mismatch: %s" % bad) if bad else
                                                  ""))
    if not record["ok"]:
        print("STOPPED: %s" % record.get("stopped", "verification failed"))
    return 0 if record["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
