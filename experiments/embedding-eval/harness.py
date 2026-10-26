#!/usr/bin/env python3
"""M6b S3: the embedding test's harness. Protocol frozen in the vault brief
`90 System/briefs/M6b-embedding-eval.md`; expected passages frozen by D124 (Q94 (a)).

    "$HOME/Library/Application Support/dbrain/venvs/m6b-py312/bin/python" \
        experiments/embedding-eval/harness.py --out artifacts/m6b/<run-id>

Three arms (protocol item 1), each ranking the same 26 frozen chunks for the same frozen
cases, with no generation step:
  A  the lexical baseline exactly as retrieval-v1 scored it (BM25-style, its stop words);
  B  google/embeddinggemma-2, text only, float32, the model's own "query" and "document"
     prompts, at 768 dimensions and at 256 re-normalised;
  C  Qwen/Qwen3-Embedding-0.6B, float32, its own "query" instruction, full dimension.

Inputs are read, never written: dev.json and corpus.json from the 1 Oct evidence archive,
each checked against its archived SHA-256 before use. The reserved acceptance cases are
never opened (item 2). Model files are checked against manifest.json before loading, and
loaded from the local folder only. Each arm runs in its own process under sandbox-exec with
no-network.sb (item 6), and refuses to start unless a probe shows outbound traffic denied;
inside, Python's socket and resolver calls also raise while ranking. Every ranking is saved
whole, with its scores, and every output file's SHA-256 goes into hashes.json (item 6: no
run counts without them). One process per arm also makes each arm's peak RSS its own.

Metrics (item 4): per case, recall at 3 and at 5 of the D124 labels (one label in the top k
counts; media_storage accepts C11 or C22), and wrong-evidence hits in the top 3 (top-3
chunks that are not a label; for a case with no label, every returned chunk). Arm A, like
retrieval-v1, returns only chunks that share a term with the question; B and C always
return k chunks. source_retention's rule is outside the corpus: reported apart, not in the
totals. Cold and warm load time and peak RSS are recorded, not used to select (item 4):
cold is the first load in a fresh process, warm a reload after the first model is released.
"""
import argparse
import collections
import gc
import hashlib
import json
import math
import os
import platform
import re
import resource
import socket
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent.parent
sys.path.insert(0, str(RUNTIME))
from dbrain import outpath  # noqa: E402

FROZEN = (Path.home() / "Documents" / "Source_Material" / "AI_Second_Brain" / "local-models"
          / "2026-10-01" / "benchmark" / "retrieval-v1")
# From the archive's artifact_hashes.json (1 Oct). A file that differs is not the frozen one.
FROZEN_HASHES = {
    "dev.json": "38d7f6d0526c2e1bb460f2d97523133416c3786400e0b0f00c2644ce0eef50a3",
    "corpus.json": "579ee9d240c113919c817af63966af85388177e0c26d08edb7cb38d0c39a3875",
}
RESERVED = "acceptance-reserved.json"          # item 2: never used, never opened
MODELS = Path.home() / "Library" / "Application Support" / "dbrain" / "models" / "embeddings"
MANIFEST = HERE / "manifest.json"

# D124 (David, 9 Oct, Q94 (a)): the frozen expected passages. A set means any one counts.
LABELS = {
    "local_stage": {"C19"},
    "client_policy": {"C9"},
    "write_approval": {"C19"},
    "media_storage": {"C11", "C22"},
    "query_navigation": {"C5"},
    "source_retention": set(),      # rule outside the corpus: reported apart, not scored
    "remote_voice": {"C20"},
    "french_voice": {"C20"},
    "unknown_gpu": set(),           # unanswerable: only wrong hits count
    "unknown_password": set(),
}
REPORTED_APART = {"source_retention"}
KS = (3, 5)


class FrozenInputError(Exception):
    """An input is not the frozen one (hash, case list or labels)."""


# -- inputs -----------------------------------------------------------------------------
def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def load_frozen(folder=FROZEN, hashes=None, labels=None):
    """(cases, chunks, input_hashes): dev.json and corpus.json, read once as bytes and
    checked against their archived SHA-256, then parsed from those same bytes. The case
    ids must be exactly the labelled ones, and every label must be a chunk id."""
    hashes = FROZEN_HASHES if hashes is None else hashes
    labels = LABELS if labels is None else labels
    folder = Path(folder)
    got = {}
    data = {}
    for name in ("dev.json", "corpus.json"):      # only these two; RESERVED is never opened
        raw = (folder / name).read_bytes()
        got[name] = _sha256_bytes(raw)
        if got[name] != hashes[name]:
            raise FrozenInputError("%s is not the frozen file (sha256 %s)" % (name, got[name]))
        data[name] = json.loads(raw.decode("utf-8"))
    cases = data["dev.json"]
    chunks = data["corpus.json"]["chunks"]
    ids = [c["id"] for c in cases]
    if sorted(ids) != sorted(labels) or len(set(ids)) != len(ids):
        raise FrozenInputError("the case list is not the labelled one")
    chunk_ids = {c["id"] for c in chunks}
    if len(chunk_ids) != len(chunks):
        raise FrozenInputError("duplicate chunk ids")
    missing = sorted(l for ls in labels.values() for l in ls if l not in chunk_ids)
    if missing:
        raise FrozenInputError("labels that are not chunks: %s" % ", ".join(missing))
    return cases, chunks, got


def labels_digest(labels=None):
    labels = LABELS if labels is None else labels
    canon = json.dumps({k: sorted(v) for k, v in sorted(labels.items())}, sort_keys=True)
    return _sha256_bytes(canon.encode("utf-8"))


# -- arm A: the lexical baseline, as retrieval-v1 ------------------------------------------
# Copied from the archived retrieval.py (sha256 84d68743…, tested against it): same stop
# words, tokens and scoring; ties broken by chunk id. Only the cut-off differs: the whole
# ranking of matching chunks is returned, so recall at 5 can be read too.
STOP = set("a an the is are does do what which how when before after to of for in on and or "
           "with it its this that can must be should i my we as at by from".split())


def words(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z0-9]+", s) if w not in STOP and len(w) > 1]


def lexical_ranking(chunks, question):
    counts = [collections.Counter(words(c["heading"] + " " + c["text"])) for c in chunks]
    avg = sum(sum(c.values()) for c in counts) / len(counts)
    terms = set(words(question))
    scores = []
    for c, tf in zip(chunks, counts):
        score = 0
        for t in terms:
            df = sum(t in x for x in counts)
            idf = math.log(1 + (len(chunks) - df + .5) / (df + .5))
            f = tf[t]
            score += idf * f * 2.5 / (f + 1.5 * (.25 + .75 * sum(tf.values()) / avg)) if f else 0
        if score > 0:
            scores.append((score, c))
    return [(c["id"], round(s, 5)) for s, c in sorted(scores, key=lambda x: (-x[0], x[1]["id"]))]


class LexicalArm:
    name = "A-lexical"

    def describe(self):
        return {"arm": self.name, "kind": "lexical", "source": "retrieval-v1 retrieval.py"}

    def load(self, verify=True):
        return 0.0

    def rank(self, chunks, cases):
        return {c["id"]: lexical_ranking(chunks, c["question"]) for c in cases}


# -- arms B and C: embeddings -------------------------------------------------------------
def chunk_input(chunk):
    """What an embedding arm reads for a chunk: the same heading and text arm A scores."""
    return chunk["heading"] + "\n" + chunk["text"]


def _normalise(vec):
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n else list(vec)


def cosine_ranking(query_vec, doc_vecs, ids, dim=None):
    """Chunk ids by cosine similarity, highest first, ties by id. With `dim`, vectors are
    cut to their first `dim` values and re-normalised (Matryoshka truncation). Sorted on
    the full-precision score; the score is rounded only for the saved ranking."""
    q = _normalise(query_vec[:dim] if dim else query_vec)
    out = []
    for cid, d in zip(ids, doc_vecs):
        d = _normalise(d[:dim] if dim else d)
        out.append((cid, sum(a * b for a, b in zip(q, d))))
    return [(cid, round(s, 6)) for cid, s in sorted(out, key=lambda x: (-x[1], x[0]))]


def model_folder(repository, revision):
    org, name = repository.split("/")
    return MODELS / ("%s__%s@%s" % (org, name, revision[:12]))


def verify_model(entry, folder=None):
    """Every manifest file present with its size and SHA-256 (item 5); raises otherwise."""
    folder = Path(folder) if folder else model_folder(entry["repository"], entry["revision"])
    for f in entry["files"]:
        p = folder / f["path"]
        if not p.is_file() or p.stat().st_size != f["size"] or sha256_file(p) != f["sha256"]:
            raise FrozenInputError("%s: %s does not match the manifest" % (entry["repository"], f["path"]))
    return folder


# EmbeddingGemma 2's model card, "Selective Encoder Loading": text only (item 1, text path).
GEMMA_TEXT_ONLY = {"vision_config": None, "audio_config": None}


def _sentence_transformer(folder, config_kwargs=None):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer
    import torch
    kwargs = {"device": "cpu", "local_files_only": True, "model_kwargs": {"dtype": torch.float32}}
    if config_kwargs:
        kwargs["config_kwargs"] = dict(config_kwargs)
    return SentenceTransformer(str(folder), **kwargs)


class EmbeddingArm:
    """One model, possibly read at several dimensions (each dimension is its own arm in
    the results). `loader(folder)` returns an object with encode(texts, prompt_name=...)."""

    def __init__(self, name, repository, dims, loader=None, folder=None, manifest_entry=None,
                 config_kwargs=None):
        self.name, self.repository, self.dims = name, repository, dims
        self.config_kwargs = config_kwargs
        self.loader = loader or (lambda f: _sentence_transformer(f, config_kwargs))
        self.folder, self.manifest_entry = folder, manifest_entry
        self.model = None

    def describe(self):
        d = {"arm": self.name, "kind": "embedding", "repository": self.repository,
             "dims": list(self.dims), "prompts": ["query", "document"], "dtype": "float32",
             "device": "cpu", "config_kwargs": self.config_kwargs}
        if self.manifest_entry:
            d["revision"] = self.manifest_entry["revision"]
        return d

    def load(self, verify=True):
        """Load from the local folder; with `verify`, check every file first (the warm
        reload skips it: the files were checked a moment ago and are timed apart)."""
        folder = self.folder
        if self.manifest_entry is not None:
            folder = (verify_model(self.manifest_entry, folder) if verify
                      else Path(folder or model_folder(self.repository, self.manifest_entry["revision"])))
        t0 = time.perf_counter()
        self.model = self.loader(folder)
        return round(time.perf_counter() - t0, 3)

    def release(self):
        self.model = None
        gc.collect()

    def _encode(self, texts, prompt_name):
        vecs = self.model.encode(texts, prompt_name=prompt_name, normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]

    def rank(self, chunks, cases):
        ids = [c["id"] for c in chunks]
        docs = self._encode([chunk_input(c) for c in chunks], "document")
        queries = self._encode([c["question"] for c in cases], "query")
        out = {}
        for dim in self.dims:
            key = "%s@%s" % (self.name, dim or "full")
            out[key] = {c["id"]: cosine_ranking(q, docs, ids, dim) for c, q in zip(cases, queries)}
        return out


# -- the network block (item 6) ---------------------------------------------------------
# Two layers. The boundary is the process: every arm runs in its own process under
# sandbox-exec with NO_NETWORK_PROFILE (all outbound traffic denied, Unix sockets included,
# so the system resolver cannot be reached either), and the worker refuses to start unless
# a probe shows the denial is in force. Inside it, NoNetwork also makes Python's socket and
# resolver calls raise during ranking, so an attempt shows as an error, not a hang.
NO_NETWORK_PROFILE = HERE / "no-network.sb"


class NetworkBlocked(OSError):
    pass


class NotSandboxed(Exception):
    """The worker is not running under the no-network sandbox."""


def network_denied():
    """True when an outbound connection is refused by the sandbox (EPERM), not by the
    peer: a loopback port with nothing listening answers ECONNREFUSED when the network is
    allowed. Name resolution must fail too."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 9))
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    finally:
        s.close()
    try:
        socket.getaddrinfo("example.com", 443)
        return False
    except OSError:
        return True


_SOCKET_ATTRS = ("socket", "create_connection", "getaddrinfo", "gethostbyname",
                 "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "socketpair", "fromfd")
_METHODS = ("connect", "connect_ex", "sendto", "sendmsg")


class NoNetwork:
    """While active, creating a socket, connecting or sending on one, or any name lookup
    through Python's socket module raises NetworkBlocked."""

    def __enter__(self):
        def refuse(*a, **kw):
            raise NetworkBlocked("network blocked during scoring (M6b item 6)")
        self._cls = socket.socket
        self._saved_methods = {m: getattr(self._cls, m) for m in _METHODS}
        self._saved_attrs = {a: getattr(socket, a) for a in _SOCKET_ATTRS if hasattr(socket, a)}
        for m in _METHODS:
            setattr(self._cls, m, refuse)
        for a in self._saved_attrs:
            setattr(socket, a, refuse)
        return self

    def __exit__(self, *exc):
        for a, v in self._saved_attrs.items():
            setattr(socket, a, v)
        for m, v in self._saved_methods.items():
            setattr(self._cls, m, v)
        return False


# -- metrics (item 4) -------------------------------------------------------------------
def score_case(case_id, ranking, labels=None):
    labels = LABELS if labels is None else labels
    want = labels[case_id]
    top = [cid for cid, _ in ranking]
    row = {"case": case_id, "top5": top[:5],
           "wrong_top3": sum(1 for cid in top[:3] if cid not in want),
           "reported_apart": case_id in REPORTED_APART}
    for k in KS:
        row["recall@%d" % k] = (1 if want & set(top[:k]) else 0) if want else None
    return row


def totals(rows):
    scored = [r for r in rows if not r["reported_apart"]]
    out = {"cases": len(scored), "wrong_top3": sum(r["wrong_top3"] for r in scored),
           "wrong_top3_answerable": sum(r["wrong_top3"] for r in scored if r["recall@3"] is not None),
           "wrong_top3_unanswerable": sum(r["wrong_top3"] for r in scored if r["recall@3"] is None)}
    answerable = [r for r in scored if r["recall@3"] is not None]
    out["answerable"] = len(answerable)
    for k in KS:
        out["recall@%d" % k] = sum(r["recall@%d" % k] for r in answerable)
    return out


# -- one arm, in its own process ---------------------------------------------------------
def _peak_rss_bytes():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss if sys.platform == "darwin" else rss * 1024


def _write_json(path, obj):
    with outpath.open_for_write(path) as fh:
        fh.write(json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def arm_by_name(name, manifest_path=MANIFEST):
    if name == "A":
        return LexicalArm()
    manifest = json.loads(Path(manifest_path).read_text())
    by_repo = {m["repository"]: m for m in manifest["models"]}
    if name == "B":
        gemma = by_repo["google/embeddinggemma-2"]
        return EmbeddingArm("B-embeddinggemma-2", gemma["repository"], (768, 256),
                            manifest_entry=gemma, config_kwargs=GEMMA_TEXT_ONLY)
    if name == "C":
        qwen = by_repo["Qwen/Qwen3-Embedding-0.6B"]
        return EmbeddingArm("C-qwen3-embedding-0.6b", qwen["repository"], (None,),
                            manifest_entry=qwen)
    raise ValueError("arm: A, B or C")


def worker(arm, frozen, hashes, labels, out_file, require_sandbox=True):
    """Score one arm in this process and write its rankings and measurements. Load (local
    files, checked first) → rank with the network blocked → release → warm reload →
    release. Peak RSS is this process's, read at the end, so it covers loading and ranking
    of this arm alone."""
    if require_sandbox and not network_denied():
        raise NotSandboxed("run under sandbox-exec -f %s" % NO_NETWORK_PROFILE)
    if os.path.lexists(out_file):
        raise FileExistsError("refusing to overwrite %s" % out_file)
    cases, chunks, got = load_frozen(frozen, hashes, labels)
    cold = arm.load()
    with NoNetwork():
        ranked = arm.rank(chunks, cases)
    warm = 0.0
    if isinstance(arm, EmbeddingArm):
        arm.release()
        warm = arm.load(verify=False)
        arm.release()
    variants = ranked if isinstance(arm, EmbeddingArm) else {arm.name: ranked}
    result = {"describe": arm.describe(), "inputs": got, "variants": variants,
              "load": {"cold_s": cold, "warm_s": warm, "peak_rss_bytes": _peak_rss_bytes()},
              "sandboxed": bool(require_sandbox), "python": platform.python_version()}
    _write_json(out_file, result)
    return result


def _spawn(name, frozen, hashes, labels, out_file):
    argv = ["sandbox-exec", "-f", str(NO_NETWORK_PROFILE), sys.executable, str(Path(__file__).resolve()),
            "worker", "--arm", name, "--frozen", str(frozen), "--hashes", json.dumps(hashes),
            "--labels", json.dumps({k: sorted(v) for k, v in labels.items()}), "--out", str(out_file)]
    subprocess.run(argv, check=True)


# -- a run ------------------------------------------------------------------------------
_VARIANT = re.compile(r"[A-Za-z0-9.-]+@[A-Za-z0-9]+")


def _check_worker(result, hashes, case_ids, chunk_ids, require_sandboxed):
    """A worker's file is used only if it read the frozen inputs, ran sandboxed, and holds a
    ranking for exactly the frozen cases, of known chunk ids, under safe variant names."""
    if result.get("inputs") != hashes:
        raise FrozenInputError("a worker read other inputs")
    if require_sandboxed and result.get("sandboxed") is not True:
        raise NotSandboxed("a worker did not run sandboxed")
    variants = result.get("variants")
    if not (isinstance(variants, dict) and variants):
        raise FrozenInputError("a worker returned no rankings")
    arm = result.get("describe", {}).get("arm")
    for key, per_case in variants.items():
        name = key if "@" in key else key + "@all"
        if not (_VARIANT.fullmatch(name) and key.split("@")[0] == arm):
            raise FrozenInputError("a worker returned an unexpected variant name")
        if not isinstance(per_case, dict) or sorted(per_case) != sorted(case_ids):
            raise FrozenInputError("a worker's rankings do not cover exactly the frozen cases")
        for ranking in per_case.values():
            ids = [r[0] for r in ranking]
            if len(set(ids)) != len(ids) or not set(ids) <= chunk_ids or not all(
                    isinstance(r[1], (int, float)) and not isinstance(r[1], bool) for r in ranking):
                raise FrozenInputError("a worker's ranking holds unknown or repeated chunks")


def run(arm_names, out_dir, frozen=FROZEN, hashes=None, labels=None, spawn=_spawn,
        require_sandboxed=True):
    """One sandboxed process per arm, then the scores. Writes into a fresh out_dir: each
    worker's file, each ranking, scores.json and hashes.json (every other file's SHA-256).
    Returns the summary. `require_sandboxed=False` only for tests that run the worker in
    this process."""
    hashes = FROZEN_HASHES if hashes is None else hashes
    labels = LABELS if labels is None else labels
    cases, chunks, _ = load_frozen(frozen, hashes, labels)
    case_ids, chunk_ids = [c["id"] for c in cases], {c["id"] for c in chunks}
    out_dir = outpath.make_dir(Path(out_dir), fresh=True)
    summary = {"inputs": dict(hashes, labels=labels_digest(labels)), "arms": {}, "load": {}}
    written = {}
    for name in arm_names:
        wfile = out_dir / ("worker-%s.json" % name)
        spawn(name, frozen, hashes, labels, wfile)
        result = json.loads(wfile.read_text())
        _check_worker(result, hashes, case_ids, chunk_ids, require_sandboxed)
        written[wfile.name] = sha256_file(wfile)
        summary["load"][result["describe"]["arm"]] = dict(result["load"], sandboxed=result["sandboxed"])
        for key, per_case in sorted(result["variants"].items()):
            rows = [score_case(cid, per_case[cid], labels) for cid in sorted(per_case)]
            summary["arms"][key] = {"describe": result["describe"], "totals": totals(rows), "cases": rows}
            rname = "rankings-%s.json" % key.replace("@", "-")
            _write_json(out_dir / rname, {"arm": key, "rankings": per_case})
            written[rname] = sha256_file(out_dir / rname)
    _write_json(out_dir / "scores.json", summary)
    written["scores.json"] = sha256_file(out_dir / "scores.json")
    _write_json(out_dir / "hashes.json", written)
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    w = sub.add_parser("worker", help="internal: one arm, in its own sandboxed process")
    w.add_argument("--arm", required=True, choices=("A", "B", "C"))
    w.add_argument("--frozen", required=True, type=Path)
    w.add_argument("--hashes", required=True)
    w.add_argument("--labels", required=True)
    w.add_argument("--out", required=True, type=Path)
    ap.add_argument("--out", type=Path, help="a new folder, outside the vault")
    args = ap.parse_args(argv)
    if args.cmd == "worker":
        labels = {k: set(v) for k, v in json.loads(args.labels).items()}
        worker(arm_by_name(args.arm), args.frozen, json.loads(args.hashes), labels, args.out)
        return
    if not args.out:
        ap.error("--out is required")
    summary = run(("A", "B", "C"), args.out)
    print(json.dumps({k: v["totals"] for k, v in summary["arms"].items()}, indent=1))


if __name__ == "__main__":
    main()
