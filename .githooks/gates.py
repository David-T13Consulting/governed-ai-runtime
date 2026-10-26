"""Commit gates for David's two repositories (AS1 step 3, D131, Q107).

The shims in this folder run it once `core.hooksPath` is `.githooks` (`gates.py install`):

  pre-commit        the staged content: a secret scan; in the vault, each note's classification
  commit-msg        the same checks again, plus the protected-file gate, which needs the message
  pre-merge-commit  refuses merge commits (linear history, D50)
  pre-push          every outgoing commit, against its parent: the same checks, its own
                    message's approval trailer, no merge commits. This catches commits made
                    without the commit hooks: rebase, cherry-pick, am, plumbing, --no-verify.
  install, check    set `core.hooksPath` and verify the installation

Contract (David, Q107, 14 Oct 2026): the gates stop accidental landings. They cannot stop an
agent that deliberately disables them (`--no-verify` on both commit and push, an unset or
overridden `core.hooksPath`, an edited gate); AGENTS.md forbids that and the startup check
(AS1 step 4) detects it. Every error fails closed (exit 2). Nothing a pattern matched is
printed, and neither is git's own error output.

The same file runs in both repositories; the shims pass `--repo vault` or `--repo runtime`.
Standard library only, Python 3.9.
"""
import datetime
import os
import unicodedata
import re
import subprocess
import sys

# Replacement objects (`git replace`) change what git shows but not what a push sends, so
# every git call here reads the real objects (D74 consult, 19 Oct).
os.environ["GIT_NO_REPLACE_OBJECTS"] = "1"

# The patterns of the runtime's `secret_scan.sh`, written exactly as there (POSIX ERE, one
# per line, matched per line and case-sensitively); a runtime test checks both lists are
# identical and flag the same lines. Unlike that scan, binary content is scanned too.
PATTERNS = (
    ("an sk- key", r"sk-[A-Za-z0-9_-]{16,}"),
    ("a GitHub token", r"ghp_[A-Za-z0-9]{20,}"),
    ("an AWS access key", r"AKIA[0-9A-Z]{16}"),
    ("a private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("a Telegram bot token", r"[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"),
    ("a credential assignment",
     r"(password|passwd|secret|token|api[_-]?key)[[:space:]]*[:=][[:space:]]*.?[A-Za-z0-9_-]{12,}"),
)
# Matched as bytes so that binary content is scanned too, and widened to match at least
# what grep matches in a UTF-8 locale (AS1 step 3 reviews 1 and 2): `.` is one character
# of up to four bytes, so `.?` becomes `.{0,4}`; `[[:space:]]` includes Unicode spaces
# (U+00A0, U+2003...), so any multi-byte UTF-8 character counts as one. The letter ranges
# stay ASCII there, as in grep on macOS. A wider match only adds false positives.
SPACE = r"(?:[ \t\r\f\v]|[\xc2-\xf4][\x80-\xbf]{1,3})"
COMPILED = tuple((name, re.compile(ere.replace("[[:space:]]", SPACE)
                                   .replace(".?", ".{0,4}").encode("ascii")))
                 for name, ere in PATTERNS)

# Protected paths (D131): a change to any of them needs a `David-approved:` trailer naming a
# recorded approval. Matched on every path component, case-folded (macOS file names).
PROTECTED_NAMES = ("agents.md", "claude.md", ".claude", ".githooks")

VAULT_DECISIONS = "10 Projects/AI Second Brain/DECISIONS.md"
VAULT_FOR_DAVID = "90 System/for-david.md"
VAULT_ARCHIVE = "90 System/for-david-archive.md"
DEFAULT_VAULT = os.path.expanduser(os.environ.get("DBRAIN_VAULT", "~/Work/vault"))

HOOKS = ("pre-commit", "commit-msg", "pre-merge-commit", "pre-push")
REGULAR, EXECUTABLE, SYMLINK, GITLINK = b"100644", b"100755", b"120000", b"160000"
ZERO = re.compile(r"0+")
MAX_LINES = 50


class GateError(Exception):
    """Something the gate cannot judge: the commit or push is refused (fail closed)."""


def git(args, cwd=None, env=None, data=None, ok=(0,)):
    """Run git; its output as bytes. Any other exit status is a GateError, and git's own
    error text is never printed (it can quote file content or paths)."""
    proc = subprocess.run(["git"] + list(args), cwd=cwd, env=env, input=data,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode not in ok:
        raise GateError("git %s failed" % args[0])
    return proc.stdout


def foreign_env():
    """The environment for git in another repository: none of this repository's GIT_*
    settings (a hook runs with GIT_INDEX_FILE, GIT_DIR and others set), real objects only."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def scan(data):
    """(line number, pattern name) for each line of `data` (bytes) a pattern matches."""
    hits = []
    for number, line in enumerate(data.split(b"\n"), 1):
        for name, rx in COMPILED:
            if rx.search(line):
                hits.append((number, name))
    return hits


def shown(path):
    """A path fit to print: withheld if it looks like a credential itself."""
    if scan(path):
        return "<a path withheld: it looks like a credential>"
    return path.decode("utf-8", "surrogateescape").encode("ascii", "backslashreplace").decode()


def is_protected(path):
    parts = [p.casefold() for p in path.decode("utf-8", "surrogateescape").split("/")]
    name = parts[-1]
    return (any(p in PROTECTED_NAMES for p in parts) or name == ".gitignore"
            or name.endswith(".sb") or name == "secret_scan.sh"
            or (name.startswith("data-policy") and name.endswith(".md")))


def parse_raw(out):
    """Raw `-z --no-renames` diff output -> (status, new mode, new object id, path) records.
    An unmerged entry or any status but A, D, M, T is an error."""
    records, tokens = [], out.split(b"\0")
    i = 0
    while i < len(tokens) and tokens[i]:
        meta = tokens[i]
        if not meta.startswith(b":") or i + 1 >= len(tokens):
            raise GateError("unexpected diff output")
        fields = meta[1:].split(b" ")
        if len(fields) != 5:
            raise GateError("unexpected diff output")
        status = fields[4][:1]
        if status not in (b"A", b"D", b"M", b"T"):
            raise GateError("an unmerged or unexpected change")
        records.append((status, fields[1], fields[3], tokens[i + 1]))
        i += 2
    return records


def empty_tree():
    return git(["hash-object", "-t", "tree", "/dev/null"]).strip().decode()


def head():
    out = git(["rev-parse", "--verify", "--quiet", "HEAD"], ok=(0, 1)).strip()
    return out.decode() if out else None


def staged_changes():
    """The changes the commit being made would record: the index against HEAD, through
    whatever index git gave this hook (a path-limited commit uses its own)."""
    return parse_raw(git(["diff-index", "--cached", "-z", "--no-renames", head() or empty_tree()]))


def commit_changes(commit, parent):
    return parse_raw(git(["diff-tree", "-r", "-z", "--no-renames", parent or empty_tree(), commit]))


# AS1 step 3, after review 5 and its D74 consult (15 Oct, session 49): a closed grammar, not
# an approximation of YAML. Every frontmatter line is blank or `key: value` at the left
# margin; a value is a plain scalar, one quoted string, or a one-line flow list of such
# items, optionally followed by a comment. Anything else (indentation, list items,
# comment lines, anchors, multi-line values) is refused with a request to reformat. Every
# vault note fits (15 Oct) but four whose plain value holds ": ", which YAML itself
# rejects; they need quoting when next changed.
# A key starts with a letter or "_" and has at most 64 characters (review 3 of 15 Oct: a
# key read as a date or number, or over YAML's 1,024-character simple-key limit, broke it).
_KEY = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
# Only the escapes the vault uses, \" and \\ (reviews 1 and 2 of 15 Oct: "\z" and
# "\U00110000" made PyYAML fail).
_DQ = r'"(?:[^"\\\n]|\\["\\])*"'
_SQ = r"'(?:[^'\n]|'')*'"
# A plain value: no YAML indicator or space first (review 1 of 15 Oct: a second space let it
# start before a quote), not a bare "=" (YAML 1.1's value tag; found by fuzzing, 15 Oct),
# no ": " or " #" inside, no trailing ":" or space.
_PLAIN = r"(?![-?:,\[\]{}#&*!|>'\"%@` ])(?!=(?: |$))(?:[^:#\n]|:(?! |$)|(?<! )#)*(?<![ :])"
# An unquoted list item: letters, digits, spaces and - / ( ) . _ only, as the vault writes
# them; anything else must be quoted (fuzzing, 15 Oct: "[=??]", "[~?-]" read otherwise).
_ITEM = r"(?:%s|%s|[\w/(](?:[\w ./()-]*[\w./()-])?)" % (_DQ, _SQ)
_FLOW = r"\[ *(?:%s(?: *, *%s)*)? *\]" % (_ITEM, _ITEM)
# A comment after the value needs a space before its "#" ("public#x" is one value).
FRONT_LINE = re.compile(r"(?:%s:(?: +(?P<value>%s|%s|%s|%s))?(?: +#.*)?)?" % (_KEY, _DQ, _SQ, _FLOW, _PLAIN))
_FLOW_ITEM = re.compile(_ITEM)
# Plain scalars YAML 1.1 gives a meaning of its own that can fail to build: the value and
# merge keys, and timestamps (PyYAML's resolver pattern). Found by fuzzing, 15 Oct.
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"
                        r"(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?"
                        r"(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?")
_TIMESTAMP_PARTS = re.compile(
    r"(?P<y>[0-9]{4})-(?P<mo>[0-9]{1,2})-(?P<d>[0-9]{1,2})(?:(?:[Tt]|[ \t]+)(?P<h>[0-9]{1,2})"
    r":(?P<mi>[0-9]{2}):(?P<s>[0-9]{2})(?:\.(?P<f>[0-9]*))?(?:[ \t]*(?P<tz>Z|(?P<sign>[-+])"
    r"(?P<th>[0-9]{1,2})(?::(?P<tm>[0-9]{2}))?))?)?")


# PyYAML's own int and float patterns (resolver.py, YAML 1.1); its builders run below.
_FLOAT = re.compile(r"""[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
                    |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)
                    |\.(?:nan|NaN|NAN)""", re.X)
_INT = re.compile(r"""[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+
                  |[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+""", re.X)


def _number_problem(token):
    """True if PyYAML reads `token` as a number it then fails to build ("0x_", "0b_")."""
    is_float = _FLOAT.fullmatch(token) is not None
    if not is_float and _INT.fullmatch(token) is None:
        return False
    v = token.replace("_", "")
    v = v.lower() if is_float else v
    v = v[1:] if v[:1] in "+-" else v
    try:
        if is_float:
            if v not in (".inf", ".nan"):
                # PyYAML's base-60 sum, step for step (review 4 of 15 Oct: a long "1:1:...:1.0"
                # overflows there while each part parses).
                total, base = 0.0, 1
                for digit in reversed([float(part) for part in v.split(":")]):
                    total += digit * base
                    base *= 60
        elif v != "0":
            if v.startswith("0b"):
                int(v[2:], 2)
            elif v.startswith("0x"):
                int(v[2:], 16)
            elif v[0] == "0":
                int(v, 8)
            else:
                [int(part) for part in v.split(":")]
    except (ValueError, OverflowError):
        return True
    return False


def _special_scalar_problem(token):
    """Why YAML could not build this plain scalar, or None."""
    if token in ("=", "<<") or _number_problem(token):
        return True
    if not _TIMESTAMP.fullmatch(token):
        return None
    v = _TIMESTAMP_PARTS.fullmatch(token).groupdict()
    try:  # the checks PyYAML's timestamp constructor makes, through the same calls
        day = datetime.date(int(v["y"]), int(v["mo"]), int(v["d"]))
        if v["h"] is not None:
            tz = None
            if v["sign"]:
                delta = datetime.timedelta(hours=int(v["th"]), minutes=int(v["tm"] or 0))
                tz = datetime.timezone(-delta if v["sign"] == "-" else delta)
            datetime.datetime(day.year, day.month, day.day, int(v["h"]), int(v["mi"]),
                              int(v["s"]), int(((v["f"] or "")[:6]).ljust(6, "0")), tzinfo=tz)
    except (ValueError, OverflowError):
        return True
    return None


def _value_problem(value):
    if value is None or value[:1] in ("'", '"'):
        return False
    if value[:1] == "[":
        items = [m.group(0) for m in _FLOW_ITEM.finditer(value[1:-1])]
        return any(i[:1] not in ("'", '"') and _special_scalar_problem(i.strip()) for i in items)
    return bool(_special_scalar_problem(value))


def classification_problem(blob):
    """Why a vault note's frontmatter is not acceptable, or None. Frontmatter at the very
    top in the closed grammar above, with exactly one `classification: public` or
    `classification: internal` line, the value bare."""
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        return "is not valid UTF-8"
    lines = text.split("\n")
    if lines[0] != "---":
        return "has no frontmatter at the top"
    try:
        end = lines.index("---", 1)
    except ValueError:
        return "has frontmatter that never closes"
    front = lines[1:end]
    # Only characters YAML accepts raw (review 2 of 15 Oct: U+FFFE), and no control, format
    # or line-separator character (CR or U+2028 would be line breaks to YAML).
    for ch in "\n".join(front):
        o = ord(ch)
        printable = (0x20 <= o <= 0x7E or 0xA0 <= o <= 0xD7FF or 0xE000 <= o <= 0xFFFD
                     or 0x10000 <= o <= 0x10FFFF)
        if ch != "\n" and (not printable or unicodedata.category(ch) in ("Cf", "Zl", "Zp")):
            return "has a control or line-separator character in its frontmatter"
    for l in front:
        m = FRONT_LINE.fullmatch(l)
        if not m:
            return ("has a frontmatter line outside the accepted form (reformat it as "
                    "`key: value` at the left margin, on one line; quote a value that holds \": \")")
        if _value_problem(m.group("value")):
            return "has a frontmatter value YAML cannot read (a bad date, \"=\" or \"<<\"): quote it"
    keys = [l for l in front if l.split(":", 1)[0].lower() == "classification"]
    if not keys:
        return "has no classification"
    if len(keys) > 1:
        return "has more than one classification line"
    value = re.sub(r" +#.*", "", keys[0].split(":", 1)[1]).strip()
    if not re.fullmatch(r"classification: %s(?: +#.*)?" % re.escape(value), keys[0]):
        return "has a classification line that is not plain"
    if value.strip("\"'").lower() == "client":
        return "is classified client (a violation of the data policy)"
    if value not in ("public", "internal"):
        return "has a classification other than public or internal (bare word)"
    return None


def content_problems(repo, records):
    """Secrets in what is added or changed, its modes, and in the vault the notes' labels."""
    problems = []
    for status, mode, oid, path in records:
        if scan(path):
            problems.append("%s: the path itself looks like a credential" % shown(path))
        if status == b"D":
            continue
        if mode == GITLINK:
            problems.append("%s: a submodule; not allowed here" % shown(path))
            continue
        if mode not in (REGULAR, EXECUTABLE, SYMLINK):
            problems.append("%s: an unexpected file mode" % shown(path))
            continue
        blob = git(["cat-file", "blob", oid.decode()])   # a symlink's blob is its target text
        for number, name in scan(blob):
            problems.append("%s line %d: looks like %s" % (shown(path), number, name))
        name = path.decode("utf-8", "surrogateescape").rsplit("/", 1)[-1].casefold()
        if repo == "vault" and name.endswith(".md") and name != "claude.md":
            if mode == SYMLINK:
                problems.append("%s: a note may not be a symlink" % shown(path))
            else:
                why = classification_problem(blob)
                if why:
                    problems.append("%s %s" % (shown(path), why))
    return problems


def comment_char():
    out = git(["config", "--get", "core.commentChar"], ok=(0, 1)).decode().strip()
    return out if out and out != "auto" else "#"


def message_trailer_ids(message, cleanup=True):
    """The `David-approved:` IDs in a message's trailer block, and the values that are not
    a plain D- or Q-number. With `cleanup`, comment lines and everything below a scissors
    line go first, as git's default cleanup does."""
    lines = message.split("\n")
    if cleanup:
        cc = comment_char()
        kept = []
        for line in lines:
            if re.fullmatch(re.escape(cc) + r" -+ >8 -+", line):
                break
            if not line.startswith(cc):
                kept.append(line)
        lines = kept
    out = git(["interpret-trailers", "--parse"], data="\n".join(lines).encode("utf-8"))
    ids, bad = [], []
    for line in out.decode("utf-8", "replace").split("\n"):
        key, sep, value = line.partition(":")
        if sep and key == "David-approved":
            value = value.strip()
            (ids if re.fullmatch(r"[DQ][0-9]{1,4}", value) else bad).append(value)
    return ids, bad


def vault_dir(repo):
    if repo == "vault":
        return git(["rev-parse", "--show-toplevel"]).strip().decode(), None
    path = git(["config", "--get", "dbrain.vault"], ok=(0, 1)).strip().decode()
    return os.path.expanduser(path or DEFAULT_VAULT), foreign_env()


def section(text, heading):
    """The lines under the first `## <heading>...` header, to the next `## ` header."""
    m = re.search(r"^## %s.*$" % re.escape(heading), text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


def recorded_approvals(vault, rev, env):
    """The D-numbers in DECISIONS' table and the Q-numbers answered in for-david (and its
    archive), as committed at `rev` of the vault: never the working files or the index."""
    def read(path, required=True):
        spec = "%s:%s" % (rev, path)
        proc = subprocess.run(["git", "cat-file", "-e", spec], cwd=vault, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            if required:
                raise GateError("the vault's %s is not committed" % path.rsplit("/", 1)[-1])
            return ""
        return git(["cat-file", "blob", spec], cwd=vault, env=env).decode("utf-8", "replace")
    found = set(re.findall(r"^\|\s*(D[0-9]+)\s*\|", read(VAULT_DECISIONS), re.M))
    found |= set(re.findall(r"^- \*\*(Q[0-9]+) →", section(read(VAULT_FOR_DAVID), "Answered"), re.M))
    found |= set(re.findall(r"^\|\s*(Q[0-9]+)\s*\|", section(read(VAULT_ARCHIVE, False), "Answered"),
                            re.M))
    return found


def approval_problems(repo, records, message, evidence, cleanup):
    """The protected-file gate: a change to a protected path needs `David-approved: <ID>`
    in the message's trailers, and every ID must be recorded in the vault at the revision
    `evidence()` returns (None: no committed vault state, so no approval can count). The
    vault is only looked up when a protected path is touched."""
    touched = [path for _, _, _, path in records if is_protected(path)]
    if not touched:
        return []
    names = ", ".join(shown(p) for p in touched[:5]) + (" and more" if len(touched) > 5 else "")
    ids, bad = message_trailer_ids(message, cleanup)
    if bad:
        return ["a David-approved trailer that is not a plain D- or Q-number"]
    if not ids:
        return ["protected files changed (%s) without a David-approved: trailer naming a "
                "recorded approval (D131)" % names]
    evidence_rev = evidence()
    if evidence_rev is None:
        return ["protected files changed (%s), but there is no committed vault state to check "
                "the approval against" % names]
    vault, env = vault_dir(repo)
    if not os.path.isdir(vault):
        raise GateError("the vault is not at %s" % shown(vault.encode("utf-8", "surrogateescape")))
    known = recorded_approvals(vault, evidence_rev, env)
    missing = [i for i in ids if i not in known]
    if missing:
        return ["David-approved: %s is not a recorded decision or answered question in the "
                "vault's committed state" % ", ".join(missing)]
    return []


def vault_head(repo):
    vault, env = vault_dir(repo)
    if not os.path.isdir(vault):
        raise GateError("the vault is not at %s" % shown(vault.encode("utf-8", "surrogateescape")))
    out = git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=vault, env=env, ok=(0, 1)).strip()
    return out.decode() if out else None


def self_test():
    """A gate that cannot fail proves nothing: it must catch a planted credential and a
    planted client label before it judges anything."""
    planted = b'OPENAI_API_KEY = "' + b"sk" + b"-" + b"a" * 24 + b'"\n'
    if not scan(planted) or classification_problem(b"---\nclassification: client\n---\n") is None:
        raise GateError("the gate's self-test failed")


def refuse(problems, what):
    sys.stderr.write("COMMIT GATE: %s refused (AS1, D131):\n" % what)
    for p in problems[:MAX_LINES]:
        sys.stderr.write("  - %s\n" % p)
    if len(problems) > MAX_LINES:
        sys.stderr.write("  - ... and %d more\n" % (len(problems) - MAX_LINES))
    return 1


def message_problems(message):
    return ["the message, line %d: looks like %s" % (number, name)
            for number, name in scan(message.encode("utf-8", "surrogateescape"))]


def pre_commit(repo):
    problems = content_problems(repo, staged_changes())
    return refuse(problems, "commit") if problems else 0


def commit_msg(repo, message_file):
    with open(message_file, "rb") as f:
        raw = f.read()
    message = raw.decode("utf-8", "replace")
    records = staged_changes()
    evidence = head if repo == "vault" else (lambda: vault_head(repo))
    idents = b"\n".join(git(["var", name]) for name in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"))
    problems = (message_problems(raw.decode("utf-8", "surrogateescape"))
                + ["the author or committer, line %d: looks like %s" % hit for hit in scan(idents)]
                + content_problems(repo, records)
                + approval_problems(repo, records, message, evidence, True))
    return refuse(problems, "commit") if problems else 0


def pre_merge_commit(repo):
    return refuse(["merge commits are not allowed (linear history, D50): rebase instead"], "merge")


def pushed_tips(local):
    """The commit a pushed object leads to, and the problems on the way: an annotated tag's
    text is scanned, and a pushed tree or blob is refused (review 1: it holds content no
    commit check sees)."""
    problems, obj = [], local
    for _ in range(20):
        kind = git(["cat-file", "-t", obj]).strip()
        if kind == b"commit":
            return obj, problems
        if kind != b"tag":
            return None, problems + ["%s: pushing a %s directly is not allowed"
                                     % (local[:10], kind.decode(errors="replace"))]
        text = git(["cat-file", "tag", obj])
        problems += ["%s: the tag's text, line %d: looks like %s" % (local[:10], n, name)
                     for n, name in scan(text)]
        m = re.match(rb"object ([0-9a-f]+)\n", text)
        if not m:
            raise GateError("an unreadable tag")
        obj = m.group(1).decode()
    raise GateError("a tag chain too long")


def known_commit(sha):
    return subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0


def outgoing(lines):
    """The commits a push may send, oldest first, and the problems with the pushed objects.

    The destination is never listed: what it holds for each ref is the value git passes
    here, read over the push connection itself. Listing it again reached other places
    through stale tracking refs, a fetch URL, a remote's name and URL rewriting (reviews 5 of
    14 Oct and 4-5 of 15 Oct; D74 consult, 19 Oct). So a push may only update refs the
    destination already has, from a value known here: creating a branch or tag there is
    refused (D50: both repositories push one branch). Git also packs the tips of updates it
    rejected itself (non-fast-forward, stale lease) without passing them here (same
    consult), so every local branch and tag counts as a tip as well."""
    problems, tips, have = [], [], []
    for line in lines:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise GateError("unexpected pre-push input")
        local_ref, local, remote_ref, remote_sha = parts
        name = remote_ref.encode("utf-8", "surrogateescape")
        for ref in (local_ref, remote_ref):
            if scan(ref.encode("utf-8", "surrogateescape")):
                problems.append("a pushed ref name looks like a credential (withheld)")
        if ZERO.fullmatch(local):
            continue   # a deletion sends no commits
        if ZERO.fullmatch(remote_sha):
            problems.append("%s: creating a ref on the remote is not allowed (D50: push the "
                            "main branch only)" % shown(name))
        elif not known_commit(remote_sha):
            problems.append("%s: the remote's current commit is not here; fetch first" % shown(name))
        else:
            have.append(remote_sha)
            tips.append(local)
    if problems or not tips:
        return [], problems
    tips += git(["for-each-ref", "--format=%(objectname)", "refs/heads", "refs/tags"]).decode().split()
    commits = []
    for obj in dict.fromkeys(tips):
        tip, found = pushed_tips(obj)
        problems += found
        if tip is not None:
            commits.append(tip)
    if commits:
        commits = git(["rev-list", "--reverse", "--topo-order"] + list(dict.fromkeys(commits))
                      + ["--not"] + have).decode().split()
    return commits, problems


def history_problems():
    """A shallow repository or grafts change the history git shows here, so the commits a
    push sends could not be told apart (D74 consult, 19 Oct)."""
    problems = []
    if git(["rev-parse", "--is-shallow-repository"]).strip() != b"false":
        problems.append("a shallow repository cannot be checked: fetch its full history first")
    grafts = git(["rev-parse", "--git-path", "info/grafts"]).strip().decode("utf-8", "surrogateescape")
    if os.path.lexists(grafts):
        problems.append("the repository has grafts (.git/info/grafts): remove them first")
    return problems


def pre_push(repo, lines):
    cache = {}
    problems = history_problems()
    if problems:
        return refuse(problems, "push")
    commits, problems = outgoing(lines)

    def runtime_evidence():
        if "head" not in cache:
            cache["head"] = vault_head(repo)
        return cache["head"]
    for commit in commits:
        parents = git(["rev-list", "--parents", "-n", "1", commit]).decode().split()[1:]
        label = commit[:10]
        if len(parents) > 1:
            problems.append("%s: a merge commit (linear history, D50)" % label)
            continue
        parent = parents[0] if parents else None
        records = commit_changes(commit, parent)
        message = git(["log", "-1", "--format=%B", commit]).decode("utf-8", "replace")
        evidence = (lambda p=parent: p) if repo == "vault" else runtime_evidence
        # The whole commit object: headers (author, committer, any extra) and message.
        whole = ["the commit object, line %d: looks like %s" % hit
                 for hit in scan(git(["cat-file", "commit", commit]))]
        for p in (whole + content_problems(repo, records)
                  + approval_problems(repo, records, message, evidence, False)):
            problems.append("%s: %s" % (label, p))
    return refuse(problems, "push") if problems else 0


def check():
    """0 if this repository's gates are installed and unchanged from HEAD; else 1 and why."""
    problems = []
    top = git(["rev-parse", "--show-toplevel"]).strip().decode()
    if git(["config", "--get", "core.hooksPath"], ok=(0, 1)).strip() != b".githooks":
        problems.append("core.hooksPath is not .githooks")
    for name in HOOKS:
        path = os.path.join(top, ".githooks", name)
        if os.path.islink(path) or not os.path.isfile(path) or not os.access(path, os.X_OK):
            problems.append(".githooks/%s is missing, a symlink, or not executable" % name)
    # Compare the files themselves with HEAD's blobs, not through git's diff, which trusts
    # the index's assume-unchanged and skip-worktree flags (review 1).
    listing = subprocess.run(["git", "ls-tree", "-r", "-z", "HEAD", "--", ".githooks"], cwd=top,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tracked = {}
    if listing.returncode != 0:
        problems.append("HEAD has no .githooks")
    for entry in filter(None, listing.stdout.split(b"\0")):
        meta, _, path = entry.partition(b"\t")
        mode, _, oid = meta.split(b" ")
        tracked[path.decode("utf-8", "surrogateescape")] = (mode, oid.decode())
    for path, (mode, oid) in sorted(tracked.items()):
        full = os.path.join(top, path)
        if os.path.islink(full) or not os.path.isfile(full):
            problems.append("%s is missing or not a plain file" % shown(path.encode("utf-8", "surrogateescape")))
            continue
        with open(full, "rb") as f:
            same = f.read() == git(["cat-file", "blob", oid], cwd=top)
        if not same:
            problems.append("%s differs from HEAD" % shown(path.encode("utf-8", "surrogateescape")))
        if mode == EXECUTABLE and not os.access(full, os.X_OK):
            problems.append("%s is not executable" % shown(path.encode("utf-8", "surrogateescape")))
    for root, _, names in os.walk(os.path.join(top, ".githooks")):
        for name in names:
            rel = os.path.relpath(os.path.join(root, name), top).replace(os.sep, "/")
            if rel not in tracked:
                problems.append(".githooks holds files git does not track")
                break
    flags = git(["ls-files", "-v", "--", ".githooks"], cwd=top).decode("utf-8", "replace")
    if any(line[:1].islower() or line[:1] == "S" for line in flags.split("\n") if line):
        problems.append(".githooks has files marked assume-unchanged or skip-worktree")
    for p in problems:
        sys.stderr.write("GATE CHECK: %s\n" % p)
    if not problems:
        sys.stdout.write("gates installed and unchanged from HEAD\n")
    return 1 if problems else 0


def install():
    top = git(["rev-parse", "--show-toplevel"]).strip().decode()
    for name in HOOKS:
        path = os.path.join(top, ".githooks", name)
        os.chmod(path, 0o755)
    git(["config", "core.hooksPath", ".githooks"], cwd=top)
    return check()


def main(argv):
    if len(argv) < 3 or argv[0] != "--repo" or argv[1] not in ("vault", "runtime"):
        sys.stderr.write("usage: gates.py --repo vault|runtime <hook or install|check> [args]\n")
        return 2
    repo, action, rest = argv[1], argv[2], argv[3:]
    try:
        self_test()
        if action == "pre-commit":
            return pre_commit(repo)
        if action == "commit-msg" and len(rest) == 1:
            return commit_msg(repo, rest[0])
        if action == "pre-merge-commit":
            return pre_merge_commit(repo)
        if action == "pre-push" and len(rest) == 2 and rest[1]:
            # Git passes the remote's name and URL; neither is used (see `outgoing`), but a
            # call without them is not git's.
            return pre_push(repo, sys.stdin.read().split("\n"))
        if action == "check":
            return check()
        if action == "install":
            return install()
        sys.stderr.write("gates.py: unknown action\n")
        return 2
    except Exception as exc:  # fail closed, and print only the kind of failure
        detail = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        if scan(detail.encode("utf-8", "replace")):
            detail = "an error whose text looks like a credential (withheld)"
        sys.stderr.write("COMMIT GATE ERROR: %s; refused (fail closed, AS1)\n" % detail)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
