"""Chunk allowed notes into citable spans. Agentic file search - no vector store (D5)."""
import hashlib
import json
import re

from . import config, scope

VALID_CLASSIFICATIONS = ("public", "internal", "client")

MISSING = None
MALFORMED = "malformed"
AMBIGUOUS = "ambiguous"

_KEY = re.compile(r"""^\s*(?:"classification"|'classification'|classification)\s*:\s*(.*)$""")
_BARE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*(?:#.*)?$")
_QUOTED = re.compile(r"""^(["'])(.*?)\1\s*(?:#.*)?$""")


def frontmatter_lines(raw):
    """The lines of a real YAML frontmatter block, or [] if there is not one.

    Body text is not metadata: an earlier version scanned the first 2000 characters, so a
    line in the prose could decide a note's classification.
    """
    text = raw.lstrip("\ufeff")          # a BOM must not hide the opening fence
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    out = []
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            return out
        out.append(line)
    return []                             # unterminated frontmatter: treat as absent


def classification_of(raw):
    """Read `classification` from the frontmatter. Strict: anything odd is not a label.

    Returns the lowercased value, or MISSING / MALFORMED / "ambiguous:..." - never a
    guess. Quoting is interpreted before comments, so `"internal#client"` is the single
    value `internal#client` (unrecognised, hence refused) rather than `internal`.
    """
    values = []
    for line in frontmatter_lines(raw):
        match = _KEY.match(line)
        if not match:
            continue
        rest = match.group(1).strip()
        quoted = _QUOTED.match(rest)
        if quoted:
            values.append(quoted.group(2).strip().lower())
            continue
        if rest.startswith(('"', "'")):
            return MALFORMED              # an unclosed quote is not a value
        bare = _BARE.match(rest)
        if not bare:
            return MALFORMED
        values.append(bare.group(1).strip().lower())
    if not values:
        return MISSING
    distinct = sorted(set(values))
    if len(distinct) > 1:
        return AMBIGUOUS + ":" + ",".join(distinct)
    return distinct[0]


class Chunk:
    __slots__ = ("id", "path", "start", "end", "heading", "text")

    def __init__(self, cid, path, start, end, heading, text):
        self.id = cid
        self.path = path      # vault-relative, for citations
        self.start = start    # 1-indexed inclusive line numbers
        self.end = end
        self.heading = heading
        self.text = text

    def cite(self):
        return "%s:%d-%d" % (self.path, self.start, self.end)

    def __repr__(self):
        return "Chunk(%s %s)" % (self.id, self.cite())


def _bounded(raw, start_prefix, end_prefix):
    """Return the span between two heading prefixes, plus its line offset.

    Line numbers in citations must still point at the real file, hence the offset.
    """
    lines = raw.splitlines()
    first = next((i for i, l in enumerate(lines) if l.startswith(start_prefix)), None)
    if first is None:
        # Fail closed. Returning the whole note here would silently widen the corpus the
        # moment a heading was renamed, which is the opposite of a bounded profile.
        raise scope.ScopeError(
            "section bound %r not found; refusing to index the whole note" % start_prefix
        )
    last = next((i for i, l in enumerate(lines) if i > first and l.startswith(end_prefix)),
                None)
    if last is None:
        raise scope.ScopeError(
            "section end bound %r not found after %r; refusing to index the remainder"
            % (end_prefix, start_prefix)
        )
    return "\n".join(lines[first:last]), first


def _sections(text):
    """Split on markdown headings, carrying the nearest heading with each block."""
    lines = text.splitlines()
    heading = ""
    start = 1
    buf = []
    for i, line in enumerate(lines, 1):
        if line.startswith("#"):
            if buf:
                yield heading, start, i - 1, "\n".join(buf)
            heading = line.lstrip("#").strip()
            start = i
            buf = [line]
        else:
            buf.append(line)
    if buf:
        yield heading, start, len(lines), "\n".join(buf)


class ProhibitedNote(scope.ScopeError):
    """A `client` note, or one with conflicting classifications: a policy breach that
    stops every query and is reported (AGENTS.md rules 2-3). Never skipped."""


def build(profile, vault=None, skipped=None):
    """Index every note the profile allows. Returns (chunks, files).

    With `profile.skip_unlabelled`, a note the label rules refuse is left out and
    appended to `skipped` as (path, reason) instead of raising. It is still never
    indexed, so it can never reach the model.
    """
    if vault is None:
        vault = getattr(profile, "vault", None)  # a frozen corpus (config.DEVCASE_CORPUS)
    chunks = []
    files = {}
    notes = []
    corrected = set()
    for parts in scope.iter_allowed_files(profile, vault):
        rel = scope.as_posix(parts)
        raw = scope.read_text(rel, profile, vault)
        try:
            _check_label(raw, rel)
        except ProhibitedNote:
            raise  # round E finding 5: never skipped, whatever the profile says
        except scope.ScopeError as exc:
            if not getattr(profile, "skip_unlabelled", False):
                raise
            if skipped is not None:
                skipped.append((rel, str(exc)))
            continue
        notes.append((rel, raw))
        target = correction_target(raw) if rel.startswith(INBOX_PREFIX) else None
        if target is not None:
            corrected.add(target)
    # PL1 step 7 (D143): a note named by a correction leaves answers, whatever the order the
    # files were read in. Every note was label-checked above, so a correction can never hide
    # a prohibited note: that one has already stopped the build.
    for rel, raw in notes:
        if rel in corrected:
            continue
        files[rel] = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        bound = profile.sections.get(rel)
        if bound:
            raw_for_chunks, offset = _bounded(raw, bound[0], bound[1])
        else:
            raw_for_chunks, offset = raw, 0

        for heading, start, end, text in _sections(raw_for_chunks):
            if not text.strip():
                continue
            chunks.append(
                Chunk("C%d" % (len(chunks) + 1), rel, start + offset, end + offset,
                      heading, text)
            )
    return chunks, files


# Rule 3 label check (round K redesign, after a Codex consultation, 5 Oct). Rounds E-K each
# found a new frontmatter form that the old grammar misread, because it skipped indented
# lines as "content of the key above" and fell back to itself whenever PyYAML could not
# parse. It is now an allowlist: the frontmatter must be written in the small language the
# vault actually uses (measured 5 Oct: no indented line, no CR/NEL/LS/PS/VT/FF or backslash,
# every quoted or flow value closed on its own line), the whole block is validated before
# its label counts. Invariant: every accepted entry ends on its own physical line, and exactly
# one entry authorises the note. Failure modes (David, Q46, 5 Oct): a block outside that
# language is skipped and traced (UnverifiableNote, a ScopeError), never indexed; a
# recognisable `client` label stops every query (ProhibitedNote, AGENTS.md rule 3), whether
# it sits in a valid block or in one that fails validation. Residual risk, accepted in Q46:
# a client label disguised so that it is not recognisable is skipped, not flagged; it still
# never reaches the model. Also per Q46, CRLF notes and notes with an unquoted `: ` inside a
# value (not valid YAML; four vault notes on 5 Oct) are skipped, not repaired.

_CLIENT_WORD = re.compile(r"(?<![A-Za-z0-9_-])client(?![A-Za-z0-9_-])", re.I)
# For recognising a client label in a block that fails validation (round M finding 2):
# every YAML line break counts as one, a key-like line is any spelling of `classif...:`,
# and its value runs on through more-indented lines, with no length limits.
_ANY_BREAK = re.compile("\r\n|[\r\x0b\x0c\x85\u2028\u2029]")
_KEYLIKE = re.compile(r"""^[ \t?-]*["']?classif[^:\n]*:(.*)$""", re.I)
_COMMENT_TAIL = re.compile(r"(?:^|[ \t])#.*$")


class UnverifiableNote(scope.ScopeError):
    """Frontmatter outside the allowlisted language: skipped and traced, never indexed."""
_TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)[ ]*:(?:[ ]+(.*))?$")
# Controls (CR, NEL, VT, FF...), the YAML line breaks LS and PS, a stray BOM, a tab, and the
# backslash (no escapes): none occurs in vault frontmatter, all can disguise structure.
_FORBIDDEN = re.compile("[\x00-\x1f\x7f-\x9f  ﻿\\\\]")
_COMMENT = re.compile(r"^(?:[ ]+#.*)?$")
_DQ = re.compile(r'^"([^"]*)"')
_SQ = re.compile(r"^'((?:[^']|'')*)'")
_PLAIN_START = re.compile(r"""^[^\s\-?:,\[\]{}#&*!|>'"%@`]""")
_FLOW_PLAIN = re.compile(r"""^[^\s\-?:,\[\]{}#&*!|>'"%@`][^,\[\]{}'"]*""")
LABELS = ("public", "internal")

try:  # defence in depth: the vault's own interpreter ships PyYAML; the grammar stands alone without it
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None


def _fence_split(raw):
    """(frontmatter lines, terminated?) or (None, False) when the note has no frontmatter.

    Fences are `---` (open) and `---` / `...` (close) at column 0; trailing spaces or tabs
    are allowed, as YAML allows them (round I). Only LF breaks lines (Q46); every other break
    is refused below, before it can hide a fence or a key (round K finding 2). A first line
    that starts with `---` but is not exactly a fence stops: it could be an opening fence
    disguised by such a break."""
    text = raw[1:] if raw.startswith("﻿") else raw
    lines = text.split("\n")  # plain LF only (Q46): a CR anywhere in the block is refused
    first = lines[0]
    if not first.startswith("---"):
        return None, False
    if first.rstrip(" \t") != "---":
        raise UnverifiableNote("an opening fence followed by other content")
    out = []
    for line in lines[1:]:
        if line.rstrip(" \t") in ("---", "..."):
            return out, True
        if line.startswith(("---", "...")):
            raise UnverifiableNote("a fence followed by other content")
        out.append(line)
    return out, False


def _flow_list(text, n):
    """Length of a one-line flow list of plain or quoted items at the start of `text`."""
    i = 1
    if text[i:].lstrip(" ").startswith("]"):
        return len(text) - len(text[i:].lstrip(" ")) + 1
    while True:
        while text[i:i + 1] == " ":
            i += 1
        rest = text[i:]
        m = _DQ.match(rest) or _SQ.match(rest) or _FLOW_PLAIN.match(rest)
        if not m:
            raise UnverifiableNote("a flow list outside the one-line form (line %d)" % n)
        item = m.group(0)
        if m.re is _FLOW_PLAIN and (": " in item or item.rstrip().endswith(":") or " #" in item):
            raise UnverifiableNote("a flow list item that YAML reads as structure (line %d)" % n)
        i += len(item)
        while text[i:i + 1] == " ":
            i += 1
        if text[i:i + 1] == "]":
            return i + 1
        if text[i:i + 1] != ",":
            raise UnverifiableNote("a flow list that does not close on its line (line %d)" % n)
        i += 1
        if text[i:].lstrip(" ").startswith("]"):
            raise UnverifiableNote("a flow list with an empty item (line %d)" % n)


def _value(rest, n):
    """(kind, value, needs_quoting) for the text after `key: `. The recogniser consumes the
    whole value and its comment, so nothing can continue onto the next line (round K
    finding 1). The third field is always False since Q46: a plain value containing `: `
    is not valid YAML and is refused rather than repaired."""
    rest = rest.rstrip(" ")
    if not rest or rest.startswith("#"):
        return "empty", "", False
    if rest[0] in "\"'":
        m = (_DQ if rest[0] == '"' else _SQ).match(rest)
        if not m or not _COMMENT.match(rest[m.end():]):
            raise UnverifiableNote("a quoted value that does not close on its line (line %d)" % n)
        return "quoted", m.group(1).replace("''", "'") if rest[0] == "'" else m.group(1), False
    if rest[0] == "[":
        end = _flow_list(rest, n)
        if not _COMMENT.match(rest[end:]):
            raise UnverifiableNote("content after a flow list (line %d)" % n)
        return "flow", rest[:end], False
    if not _PLAIN_START.match(rest):
        raise UnverifiableNote("a value starting with a YAML indicator (line %d)" % n)
    value = rest.split(" #", 1)[0].rstrip(" ")
    if ": " in value or value.endswith(":"):
        # Not valid YAML, so the two readers cannot agree on it (Q46): refused, not repaired.
        raise UnverifiableNote("a plain value containing `: ` (line %d)" % n)
    return "plain", value, False


def _entries(fm):
    """Validate the whole block; {key: (kind, value, needs_quoting, line)} or ProhibitedNote."""
    entries = {}
    for n, line in enumerate(fm, 2):
        if _FORBIDDEN.search(line):
            raise UnverifiableNote("a control character, line break, tab or backslash in frontmatter (line %d)" % n)
        if not line.strip(" ") or line.startswith("#"):
            continue
        if line[0] == " ":
            raise UnverifiableNote("indented frontmatter (line %d)" % n)
        m = _TOP_KEY.match(line)
        if not m:
            raise UnverifiableNote("frontmatter outside the simple key: value form (line %d)" % n)
        key = m.group(1)
        if key in entries:
            raise UnverifiableNote("duplicate frontmatter key %r (line %d)" % (key, n))
        entries[key] = _value(m.group(2) or "", n) + (line,)
    return entries


def _label_of(entries):
    """The label among validated entries, None when there is none, or ProhibitedNote. The
    rule is deliberately narrow (Q41, David, 5 Oct): only the lowercase key
    `classification` with the value `public` or `internal`, optionally quoted;
    `Classification:` or any other `classif...` key stops."""
    for key in entries:
        if key != "classification" and key.lower().startswith("classif"):
            if _CLIENT_WORD.search(entries[key][1]):
                raise ProhibitedNote("a `client` value under a key resembling classification: %r" % key)
            raise UnverifiableNote("a key resembling classification: %r" % key)
    if "classification" not in entries:
        return None
    kind, value, needs_quoting = entries["classification"][:3]
    if kind in ("plain", "quoted") and not needs_quoting and value in LABELS:
        return value
    if _CLIENT_WORD.search(value):
        raise ProhibitedNote("a `client` classification")
    raise UnverifiableNote("a classification that is not plainly public or internal")


def _grammar_label(fm):
    """The label the allowlist grammar alone reads from frontmatter lines."""
    return _label_of(_entries(fm))


class _NoDuplicates(getattr(_yaml, "SafeLoader", object)):
    pass


def _no_dup_mapping(loader, node, deep=False):
    keys = set()
    for knode, _ in node.value:
        k = loader.construct_object(knode, deep=deep)
        try:
            hash(k)
        except TypeError:  # round I: a nested complex key (`? [a, b]`) is refused, not a crash
            raise UnverifiableNote("an unhashable frontmatter key")
        if k in keys:
            raise UnverifiableNote("duplicate frontmatter key %r" % (k,))
        keys.add(k)
    return loader.construct_mapping(node, deep)


if _yaml is not None:
    _NoDuplicates.add_constructor(_yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


def _yaml_load(text):
    try:
        return True, _yaml.load(text, Loader=_NoDuplicates)
    except _yaml.YAMLError:
        return False, None


def _verified_label(raw):
    """The note's classification, verified; see _checked_label. A block that fails
    validation but reads as a client label to a human stops instead of being skipped."""
    try:
        return _checked_label(raw)
    except UnverifiableNote as exc:
        if _names_client(raw):
            raise ProhibitedNote("%s, naming a `client` classification" % exc)
        raise


def _names_client(raw):
    """True when a block that failed validation still reads, to a human, as a `client`
    label: a `classif...:` key-like line whose value, on that line or on more-indented
    lines below it, says `client` outside a comment. Only the frontmatter is read, up to
    its first `---` or `...` fence (the whole note when there is none)."""
    lines = _ANY_BREAK.sub("\n", raw.lstrip("\ufeff")).split("\n")
    if not lines[0].startswith("---"):
        return False
    region = [lines[0][3:]]
    for line in lines[1:]:
        if line.rstrip(" \t") in ("---", "..."):
            break
        region.append(line)
    for i, line in enumerate(region):
        m = _KEYLIKE.match(line)
        if not m:
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        parts = [m.group(1)]
        for nxt in region[i + 1:]:
            if not nxt.strip():
                continue  # blank lines never end a YAML value (round N finding 1)
            if len(nxt) - len(nxt.lstrip(" \t")) <= indent:
                break
            parts.append(nxt)
        if _CLIENT_WORD.search(" ".join(_COMMENT_TAIL.sub("", p) for p in parts)):
            return True
    return False


def _checked_label(raw):
    """The label, None for verified absence, UnverifiableNote or ProhibitedNote.

    1. The allowlist grammar validates the whole block and reads the label.
    2. Where PyYAML is available it must confirm that reading: same keys, same label,
       compared exactly. It can only confirm, never rescue: a parse error refuses the note
       (Q46: plain LF text that both readers parse to the same result).
    """
    fm, terminated = _fence_split(raw)
    if fm is None:
        return None
    if not terminated:
        # Round J finding 3: an opening fence with no closing fence cannot be verified.
        raise UnverifiableNote("frontmatter with no closing fence")
    entries = _entries(fm)
    label = _label_of(entries)
    if _yaml is not None:
        ok, parsed = _yaml_load("\n".join(fm))
        if not ok:
            raise UnverifiableNote("frontmatter that YAML cannot parse")
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict) or set(parsed) != set(entries):
            raise UnverifiableNote("the YAML parser reads different keys from the grammar")
        if parsed.get("classification") != label:
            raise UnverifiableNote("the YAML parser reads classification %r, the grammar %r"
                                 % (parsed.get("classification"), label))
    return label


INBOX_PREFIX = "00 Inbox/"


def correction_target(raw):
    """The path a correction note names (PL1 step 7, D143), or None. Only a note whose
    frontmatter passes the grammar, with the plain values `note_type: correction` and
    `corrects: <path>`, names one; `build` asks only of notes in `00 Inbox/` that passed
    the label check. Never raises."""
    try:
        fm, terminated = _fence_split(raw)
        if fm is None or not terminated:
            return None
        entries = _entries(fm)
    except scope.ScopeError:
        return None
    note_type, corrects = entries.get("note_type"), entries.get("corrects")
    if not (note_type and corrects and note_type[0] == "plain" and corrects[0] == "plain"):
        return None
    if note_type[1] != "correction" or not corrects[1]:
        return None
    return corrects[1]


def _check_label(raw, rel):
    """Rule 3 (AGENTS.md): only `public` and `internal` notes may reach a model. A missing
    label or an unverifiable block is a refusal the whole-vault profile may skip (Q46); a
    recognisable `client` label is a ProhibitedNote that stops every query."""
    try:
        label = _verified_label(raw)
    except ProhibitedNote as exc:
        raise ProhibitedNote("refusing a note with %s: %s" % (exc, rel))
    except UnverifiableNote as exc:
        raise UnverifiableNote("skipping a note with %s: %s" % (exc, rel))
    if label is None:
        raise scope.ScopeError("refusing a note with no `classification`: %s" % rel)
    if label not in LABELS:  # explicit final allowlist, whatever _verified_label returns
        raise ProhibitedNote("refusing a note labelled %r: %s" % (label, rel))
    return label
