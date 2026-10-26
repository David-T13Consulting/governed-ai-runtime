#!/usr/bin/env python3
"""P2.5 slice F0-F3: regression cases for "the row exists but the answer says no".

    python3 experiments/p25_regression.py --endpoint http://127.0.0.1:18089 --label f0-baseline [--dev]

Each case is asked through the phone path (`serve.Service.ask`, the prompt, date reading
and status the phone sees) on a synthetic vault built in a fresh folder under /private/tmp,
on the fixed test date of the case file.

Scoring is a whole-answer contract with three outcomes (Codex D74 consultation, 9 Oct,
session 45, after six review rounds each found new strings an open grammar misread). Each
case carries its ground truth (activity, date, for a row case the start time and the asked
words the note does not use), and the case file lists every calendar row of the synthetic
notes (`facts`). The answer is cut into units (sentence ends, ";", "but", "mais", line
breaks); every unit must be one claim of a small set of forms (see `_grammar`), or the
answer is **unscored**: shown for a read, kept in the count, never a pass. Claims are bound
to an activity, a date and a time and checked against the facts:

  row      a decline or another bad status fails; it passes with a "not the note's word"
           claim, a row claim for the asked activity, date and start time, no denial of
           that activity, every row claim true, and no date but the asked one
  none     a denial for the asked activity (or "nothing found") on the asked date, or the
  other    service's decline text with status `withheld`; no row claim; no other date
  negated  as none, but a decline fails (the note does say)

The lead's reads of actual answers are kept in p25_adjudication.json, by answer SHA-256,
apart from the automatic outcome; a changed or unscored answer is listed as needing a read.
`--rescore <report>` scores a saved report's answers again with the current scorer, so a
baseline and a later run are judged alike.

With --dev the 10 dev-case questions are also asked through the phone path, on the
three-file dev corpus (read-only), so a change to the phone's prompt shows up there too.
Reports go to artifacts/p25/<label>.json through the write guard. Synthetic text only:
the case file holds no wording from David's phone (brief P2.5, step F0).
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import uuid
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dbrain import adapter as adapter_mod  # noqa: E402
from dbrain import config, outpath, serve  # noqa: E402

CASES = HERE / "p25_cases.json"
# The lead's reads of actual answers, keyed by case and the answer's SHA-256, so a changed
# answer needs a new read. Kept apart from the automatic outcome, never merged into it.
ADJUDICATION = HERE / "p25_adjudication.json"
TOKEN = "p25-regression-" + "x" * 32
TYPES = ("row", "none", "negated", "other")
ANSWERED = ("supported", "incomplete")
# The synthetic vault's parent: fixed, never the ambient TMPDIR (Codex round 4), created
# through outpath's anchored walk, which refuses the vault.
TMP_PARENT = Path("/private/tmp")
TMP_PREFIX = "dbrain-p25-"

# The service puts "(Asked about <date>.)" in front of every dated answer; it is scored
# without it, so the date the service states never stands in for the model's.
_ASKED = re.compile(r"^\(asked about [^)]*\)\s*")
_CITE = re.compile(r"\s*\[(?:e?\d+)(?:\s*,\s*e?\d+)*\]")
OUTCOMES = ("pass", "fail", "unscored")


def load_cases(path=None):
    doc = json.loads(Path(path or CASES).read_text(encoding="utf-8"))
    acts = doc.get("activities") or {}
    owner = {}
    for act, spec in acts.items():
        for w in spec.get("aliases", []) + spec.get("groups", []):
            if owner.setdefault(w.lower(), act) != act:
                raise SystemExit("word %r names two activities" % w)
    for act, spec in acts.items():
        events = {f["event"] for f in doc.get("facts", []) if f.get("activity") == act}
        for alias, event in spec.get("specific", {}).items():
            if alias not in spec.get("aliases", []) or event not in events:
                raise SystemExit("specific alias %r of %s names no event of it" % (alias, act))
    for f in doc.get("facts", []):
        if f.get("activity") not in acts:
            raise SystemExit("fact for unknown activity %r" % f.get("activity"))
        date.fromisoformat(f["date"])
    for case in doc["cases"]:
        if case["type"] not in TYPES:
            raise SystemExit("unknown case type %r in %s" % (case["type"], case["id"]))
        if case.get("activity") not in acts or not case.get("date"):
            raise SystemExit("case %s needs a known `activity` and a `date`" % case["id"])
        date.fromisoformat(case["date"])
        for w in case.get("asked_terms", []):
            if owner.get(w.lower(), case["activity"]) != case["activity"]:
                raise SystemExit("case %s: asked term %r names another activity" % (case["id"], w))
        if case["type"] == "row" and not (case.get("expect_start") and case.get("absent_terms")):
            raise SystemExit("row case %s needs `expect_start` and `absent_terms`" % case["id"])
    alias_table(doc)  # refuses a malformed table early
    return doc


def alias_table(doc):
    """P2.5 F3: the case file's synthetic alias table, in `aliases.expand`'s form. Used only
    with --aliases; the real table (`aliases.ALIASES`) never meets the synthetic notes."""
    table = {}
    for entry in doc.get("retrieval_aliases", []):
        words, says = entry.get("words"), entry.get("notes_say")
        if not (words and says and all(isinstance(w, str) and w for w in words + says)):
            raise SystemExit("retrieval alias %r needs `words` and `notes_say`" % (entry,))
        table[tuple(words)] = tuple(says)
    return table


# -- the claim grammar ------------------------------------------------------------------
# Scoring is a whole-answer contract (Codex D74 consultation, 9 Oct, session 45). The
# answer, without the service's prefix and the citation marks, is an optional date header
# and then units, split at sentence ends, ";", "but" and "mais". Every unit must be one
# claim of a form below, built only from the case file's words; a unit that is not is not
# guessed at: the answer is `unscored` (shown for a read, never a pass). Claims are bound
# to an activity, a date and a time, and checked against the synthetic notes' own rows.
#   positive  there is (also) a <event> [for the <group>] [scheduled] [on <date>] <time>;
#             the <event> [for the <group>] is (scheduled) <time> [on <date>];
#             [on <date>,] the note lists a <event>, <time>;  FR il y a (aussi) une <event> <time>
#   denial    nothing found / scheduled / listed / planned [for|on <date>];
#             there is no <event> [on <date>] [because ...]; the <event> is not scheduled
#             [on <date>]; FR il n'y a pas de / aucune <event> [<date>], il n'y a rien
#             [de prévu] [<date>], rien de prévu [pour <date>]
#   gap       the note / calendar does not (doesn't) use / mention / contain / name
#             [the word] <term>; no mention of <term> [in the note];
#             FR la note n'utilise / ne mentionne pas [le mot] <term>
#   abstain   the service's own decline text (status `withheld` only)
# <date> is a full date the reader below understands, or "this / that date / day", "ce
# jour-là" (the header's date, else the asked one). A "because" tail on a denial may hold
# only the closed forms of _reasons(). <time> is HH:MM (or 10h, 10h00) with an optional
# end.

_WD = {"mon": 0, "monday": 0, "lundi": 0, "tue": 1, "tues": 1, "tuesday": 1, "mardi": 1,
       "wed": 2, "wednesday": 2, "mercredi": 2, "thu": 3, "thur": 3, "thurs": 3,
       "thursday": 3, "jeudi": 3, "fri": 4, "friday": 4, "vendredi": 4, "sat": 5,
       "saturday": 5, "samedi": 5, "sun": 6, "sunday": 6, "dimanche": 6}
_MONTH_NAMES = {
    1: "january jan janvier janv", 2: "february feb février fevrier févr fevr",
    3: "march mar mars", 4: "april apr avril avr", 5: "may mai", 6: "june jun juin",
    7: "july jul juillet juil", 8: "august aug août aout", 9: "september sept sep septembre",
    10: "october oct octobre", 11: "november nov novembre", 12: "december dec décembre decembre déc"}
_MONTH = {w: n for n, names in _MONTH_NAMES.items() for w in names.split()}


def _alt(words):
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


_WDA, _MA = _alt(_WD), _alt(_MONTH)
_DATE = (r"(?:(?:le )?(?:(?P<{p}wd>%s),? )?(?P<{p}d1>\d{1,2})(?:st|nd|rd|th|er)? (?P<{p}m1>%s)"
         r"(?:,? (?P<{p}y1>\d{4}))?"
         r"|(?:(?P<{p}wd2>%s),? )?(?P<{p}m2>%s) (?P<{p}d2>\d{1,2})(?:st|nd|rd|th)?(?:,? (?P<{p}y2>\d{4}))?"
         r"|(?P<{p}iy>\d{4})-(?P<{p}im>\d{2})-(?P<{p}id>\d{2}))" % (_WDA, _MA, _WDA, _MA))
_DATEREF = (r"(?:%s|(?P<{p}this>(?:this|that) (?:date|day)|ce jour(?:-là)?))" % _DATE)
_TIME = (r"(?:(?:scheduled )?(?:for|from|at|de|à|a) )?(?P<start>\d{1,2}(?::\d{2}|h\d{2}|h))"
         r"(?: ?(?:-|–|to|until|à|a) ?(?P<end>\d{1,2}(?::\d{2}|h\d{2}|h)))?")
_TERM_REF = ["that word", "this word", "that term", "this term", "the word you used", "ce mot", "ce terme"]
_ABSTAIN = re.compile(r"no supporting evidence in the [\w.-]+ corpus, so i am not answering"
                      r"|widen the allowlist or ask about something the notes cover")


def _dref(p):
    return _DATEREF.replace("{p}", p)


def _date_of(m, p, year, this):
    """The date a matched <date> names: a date, or None if it is no real date (or a stated
    weekday disagrees). `this` is what "this date" stands for."""
    g = m.groupdict()
    if g.get(p + "this"):
        return this
    try:
        if g.get(p + "iy"):
            d = date(int(g[p + "iy"]), int(g[p + "im"]), int(g[p + "id"]))
            wd = None
        elif g.get(p + "d1"):
            d = date(int(g[p + "y1"] or year), _MONTH[g[p + "m1"]], int(g[p + "d1"]))
            wd = g.get(p + "wd")
        else:
            d = date(int(g[p + "y2"] or year), _MONTH[g[p + "m2"]], int(g[p + "d2"]))
            wd = g.get(p + "wd2")
    except (ValueError, TypeError):
        return None
    if wd and _WD[wd] != d.weekday():
        return None
    return d


def _hhmm(t):
    if t is None:
        return None
    m = re.fullmatch(r"(\d{1,2})(?::|h)?(\d{2})?", t)
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return "%02d:%02d" % (h, mi) if h < 24 and mi < 60 else "invalid"


def _reasons(doc, case):
    """A denial may give a reason only in closed forms built from the note's own "no event"
    row for the asked activity and date (e.g. "No rehearsal (autumn break)"): "because it
    is listed as <row>", "because it is a <why>", "due to <why>", "because it is a no
    <event> due to <why>". Anything else after a denial leaves the unit unread."""
    rows = [f for f in doc["facts"] if f["activity"] == case["activity"]
            and f["date"] == case["date"] and f.get("none")]
    if not rows:
        return ""
    row = rows[0]["event"].lower()
    m = re.fullmatch(r"(no [^(]+?)\s*\((.+)\)", row)
    why = [m.group(2)] if m else []
    what = re.escape(m.group(1)) if m else re.escape(row)
    ev = _alt([w.lower() for w in doc["activities"][case["activity"]]["aliases"]])
    w = _alt(why) if why else "(?!)"
    forms = [r"it is (?:listed|marked) as \"?%s\"?" % re.escape(row),
             r"it is (?:listed|marked) as \"?%s\"?" % what,
             r"it is (?:a |an |the )?(?:%s)" % w,
             r"it is (?:a )?no (?:%s) (?:due to|because of) (?:the )?(?:%s)" % (ev, w),
             r"(?:the )?(?:%s)" % w]
    return (r"(?P<reason> (?:because|as) (?:%s)| (?:due to|because of) (?:the )?(?:%s))?"
            % ("|".join(forms), w))


def _grammar(doc, case):
    acts = doc["activities"]
    owner = {w.lower(): a for a, s in acts.items() for w in s["aliases"]}
    for w in case.get("asked_terms", []):
        owner[w.lower()] = case["activity"]
    groups = {w.lower(): a for a, s in acts.items() for w in s.get("groups", [])}
    ev, gr = _alt(owner), _alt(groups) or "(?!)"
    terms = _alt([t.lower() for t in case.get("absent_terms", []) + case.get("asked_terms", [])]
                 + _TERM_REF)
    art = r"(?:(?:a|an|the|une?|la|le|l')\s?)?"
    group = r"(?: (?:for|of) (?:the )?(?P<group>%s))?" % gr
    on = lambda p: r"(?:,? (?:(?:on|for|pour|le) )?%s)?" % _dref(p)
    reason = _reasons(doc, case)
    q = r"[\"']?"
    forms = [
        ("pos", r"(?:(?:on )?%s,? )?there (?:is|are) (?:also )?%s(?P<event>%s)%s(?: scheduled)?%s,? %s%s"
         % (_dref("a"), art, ev, group, on("b"), _TIME, on("c"))),
        ("pos", r"%s(?P<event>%s)%s (?:is|starts|begins) (?:scheduled |on )?%s%s"
         % (art, ev, group, _TIME, on("b"))),
        ("pos", r"(?:(?:on )?%s,? )?(?:it|the note|the calendar) lists %s(?P<event>%s)%s,? %s"
         % (_dref("a"), art, ev, group, _TIME)),
        ("pos", r"il y a (?:aussi )?%s(?P<event>%s)%s%s,? %s" % (art, ev, group, on("b"), _TIME)),
        ("den", r"nothing (?:is )?(?:found|scheduled|listed|planned)%s" % on("b")),
        ("den", r"there (?:is|are) no (?P<event>%s)%s%s%s" % (ev, group, on("b"), reason)),
        ("den", r"%s(?P<event>%s)%s (?:is|are) not (?:scheduled|planned|listed|on)%s%s"
         % (art, ev, group, on("b"), reason)),
        ("den", r"(?:il n'y a )?(?:pas de|aucune?) (?P<event>%s)%s%s" % (ev, on("b"), reason)),
        ("den", r"(?:il n'y a rien|rien)(?: de (?:prévu|trouvé))?%s" % on("b")),
        ("gap", r"(?:the )?(?:note|calendar|evidence|source)s? (?:does not|doesn't|do not|don't)"
                r" (?:use|mention|contain|name) (?:the (?:word|term) )?%s(?:%s)%s(?: in the note)?" % (q, terms, q)),
        ("gap", r"(?:there is )?no mention of %s(?:%s)%s(?: in the note)?" % (q, terms, q)),
        ("gap", r"(?:la )?note (?:n'utilise|n'emploie|ne mentionne|ne contient) pas"
                r" (?:le (?:mot|terme) )?%s(?:%s)%s" % (q, terms, q)),
    ]
    header = re.compile(r"^%s:\s*" % _dref("h"))
    return [(kind, re.compile(rx)) for kind, rx in forms], header, owner, groups


def _normalise(answer):
    t = (answer or "").replace("\u2019", "'").replace("\u2018", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"').replace("«", '"').replace("»", '"')
    t = re.sub(r"[ \t]*\n\s*", " ; ", t)            # a line break ends a unit
    t = re.sub(r"\s+", " ", t).strip().lower()
    t = _ASKED.sub("", t)
    return _CITE.sub("", t).strip()


def _units(text):
    parts = re.split(r"[.!?;](?:\s+|$)|,? but |,? mais |\n", text)
    return [p.strip(" ,") for p in parts if p.strip(" ,")]


def parse(doc, case, answer):
    """(claims, unparsed units). A claim: {"kind", "activity", "date", "start", "end"}."""
    forms, header, owner, groups = _grammar(doc, case)
    asked = date.fromisoformat(case["date"])
    text = _normalise(answer)
    this = asked
    h = header.match(text)
    claims = []
    if h:
        this = _date_of(h, "h", asked.year, asked)
        claims.append({"kind": "date", "date": this})
        text = text[h.end():]
    unparsed = []
    for unit in _units(text):
        here = this
        h = header.match(unit)
        if h:
            here = _date_of(h, "h", asked.year, asked)
            claims.append({"kind": "date", "date": here})
            unit = unit[h.end():]
        if _ABSTAIN.fullmatch(unit):
            claims.append({"kind": "abstain"})
            continue
        for kind, rx in forms:
            m = rx.fullmatch(unit)
            if not m:
                continue
            g = m.groupdict()
            act = owner.get(g["event"]) if g.get("event") else None
            if g.get("reason") and act != case["activity"]:
                continue                    # the reason is the asked activity's own row only
            if g.get("group") and groups[g["group"]] != act:
                continue                    # an event for another activity's group: unread
            when = [_date_of(m, p, asked.year, here) for p in "abc"
                    if any(g.get(p + k) for k in ("d1", "d2", "iy", "this"))]
            claims.append({"kind": kind, "activity": act if kind != "gap" else None,
                           "event": g.get("event"),
                           "date": when[0] if when else here, "dates": when,
                           "start": _hhmm(g.get("start")), "end": _hhmm(g.get("end"))})
            break
        else:
            unparsed.append(unit)
    return claims, unparsed


def _fact(doc, activity, day):
    return [f for f in doc["facts"] if f["activity"] == activity and f["date"] == day.isoformat()]


def _names(doc, claim, fact):
    """Whether the claim's words can name this calendar row: a specific alias names its one
    event; a general alias (or the asked word) names any event of the activity."""
    spec = doc["activities"][claim["activity"]].get("specific", {})
    alias = (claim.get("event") or "").lower()
    return alias not in spec or spec[alias] == fact["event"]


def _true_row(doc, claim, day):
    return any("start" in f and _names(doc, claim, f) and f["start"] == claim["start"]
               and claim["end"] in (None, f.get("end")) for f in _fact(doc, claim["activity"], day))


def _false_denial(doc, claim, day, activity):
    """A denial the notes contradict: they hold an event for that activity on that day (with
    or without a time; a "no event" row is not one) that the denial's words can name."""
    c = dict(claim, activity=activity)
    return any(not f.get("none") and _names(doc, c, f) for f in _fact(doc, activity, day))


def score(case, status, answer, doc=None):
    """{"outcome": pass | fail | unscored, "reasons": [...], checks...} for one answer.
    A wrong status fails whatever the text; then any unit outside the grammar makes the
    answer unscored; otherwise the claims decide."""
    doc = doc or load_cases()
    asked = date.fromisoformat(case["date"])
    kind = case["type"]
    abstain_ok = kind in ("none", "other") and status == "withheld"
    if not (status in ANSWERED or abstain_ok):
        return {"outcome": "fail", "reasons": ["status %s" % status]}
    claims, unparsed = parse(doc, case, answer)
    if unparsed:
        return {"outcome": "unscored", "reasons": ["unread: %d unit(s)" % len(unparsed)],
                "unread": unparsed}
    reasons = []
    dated = [c for c in claims if "date" in c]
    for c in dated:
        for d in [c["date"]] + c.get("dates", []):
            if d != asked:
                reasons.append("a date other than the asked one")
    abstains = [c for c in claims if c["kind"] == "abstain"]
    if abstains and not abstain_ok:
        reasons.append("decline text with an answering status")
    if status == "withheld" and any(c["kind"] != "abstain" for c in claims):
        reasons.append("a declined answer that also claims something")
    pos = [c for c in claims if c["kind"] == "pos"]
    den = [c for c in claims if c["kind"] == "den"]
    gap = [c for c in claims if c["kind"] == "gap"]
    for c in pos:
        if not _true_row(doc, c, c["date"] or asked):
            reasons.append("a row the notes do not hold (%s)" % c["activity"])
    for c in den:                       # every denial, in every case type
        if _false_denial(doc, c, c["date"] or asked, c["activity"] or case["activity"]):
            reasons.append("denies a row the notes hold")
    case_den = [c for c in den if c["activity"] in (None, case["activity"])]
    if kind == "row":
        if status == "withheld":
            reasons.append("declined")
        if not gap:
            reasons.append("does not say the word is not the note's")
        if not any(c["activity"] == case["activity"] and c["start"] == case["expect_start"]
                   and c["date"] == asked for c in pos):
            reasons.append("does not give the asked row")
        if case_den:
            reasons.append("says no")
    else:
        if not (case_den or (abstain_ok and abstains)):
            reasons.append("does not say no")
        if pos:
            reasons.append("answers with a row")
    reasons = sorted(set(reasons))
    return {"outcome": "fail" if reasons else "pass", "reasons": reasons}


_NOTE_PATH = re.compile(r"[A-Za-z0-9 _-]+(?:/[A-Za-z0-9 _-]+)*\.md")


def build_vault(notes):
    """The synthetic notes in a fresh folder under TMP_PARENT (never the vault). Every note
    path is checked before anything is created: relative, plain names only (no "..", no
    absolute path, no dot-folder). The folder and each note are created through outpath's
    guard: anchored walk, no symlink at any depth, the vault refused by path and identity."""
    for rel in notes:
        if not _NOTE_PATH.fullmatch(rel) or any(p.strip() != p or not p.strip(" ")
                                                for p in rel.split("/")):
            raise SystemExit("refusing note path %r: plain relative .md paths only" % rel)
    root = outpath.make_dir(TMP_PARENT / (TMP_PREFIX + uuid.uuid4().hex), fresh=True)
    try:
        for rel, text in notes.items():
            with outpath.open_for_write(root / rel, strict=True) as fh:
                fh.write(text)
    except BaseException:
        remove_vault(root)
        raise
    return root


def remove_vault(root):
    """Delete a folder build_vault made, and nothing else."""
    root = Path(root)
    if root.parent != TMP_PARENT or not root.name.startswith(TMP_PREFIX) or root.is_symlink():
        raise SystemExit("refusing to remove %s: not a synthetic vault" % root)
    shutil.rmtree(str(root))


def _ask(service, question):
    code, payload = service.ask(question)
    return {
        "http": code,
        "status": payload.get("status"),
        "reason_code": payload.get(serve.REASON_KEY),
        "answer": payload.get("answer", ""),
        "status_line": payload.get("status_line"),
        "citations": [c["path"] + ":" + c["lines"] for c in payload.get("citations", [])],
        "latency_s": payload.get("latency_s"),
    }


def run(doc, model, dev=False, dev_cases=None, use_aliases=False):
    """`use_aliases`: the case file's synthetic alias table (F3); else none, the baseline."""
    today = date.fromisoformat(doc["today"])
    table = alias_table(doc) if use_aliases else {}
    root = build_vault(doc["notes"])
    try:
        profile = config.Profile("p25-synthetic", roots=("",), exclude_hidden=True,
                                 skip_unlabelled=True)
        svc = serve.Service(TOKEN, model_factory=lambda: model, profile=profile, vault=root,
                            tracing=False, today=lambda: today)
        svc.aliases = table
        rows = []
        for case in doc["cases"]:
            row = dict(case, **_ask(svc, case["question"]))
            row["score"] = score(case, row["status"], row["answer"], doc)
            row["answer_sha256"] = answer_sha256(row["status"], row["answer"])
            rows.append(row)
    finally:
        remove_vault(root)
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "today": doc["today"],
        "model": model.describe(),
        "prompt_template": serve.dated_prompt(today, []),
        "aliases": [[list(k), list(v)] for k, v in table.items()],
        "cases": rows,
        "by_type": by_type(rows),
    }
    adjudicate(report)
    if dev:
        svc = serve.Service(TOKEN, model_factory=lambda: model, profile=config.DEVCASES,
                            tracing=False, today=lambda: today)
        svc.aliases = table
        report["dev"] = [dict(id=c["id"], question=c["question"], **_ask(svc, c["question"]))
                         for c in dev_cases]
    return report


def answer_sha256(status, answer):
    return hashlib.sha256(json.dumps([status, answer], ensure_ascii=False).encode("utf-8")).hexdigest()


def by_type(rows):
    """Automatic outcomes per type over the fixed number of cases: unscored stays counted."""
    out = {}
    for t in TYPES:
        mine = [r for r in rows if r["type"] == t]
        out[t] = {o: sum(1 for r in mine if r["score"]["outcome"] == o) for o in OUTCOMES}
        out[t]["cases"] = len(mine)
    return out


def adjudicate(report, path=None):
    """Attach the lead's read to each case whose answer it was made on; list every case
    still needing a read (unscored, or no read of this exact answer)."""
    path = Path(path or ADJUDICATION)
    reads = json.loads(path.read_text(encoding="utf-8"))["reads"] if path.exists() else []
    by_key = {(r["case"], r["answer_sha256"]): r for r in reads}
    report["needs_read"] = []
    for row in report["cases"]:
        read = by_key.get((row["id"], row["answer_sha256"]))
        if read:
            row["read"] = {k: read[k] for k in ("verdict", "by", "why")}
        else:
            report["needs_read"].append(row["id"])
    return report


def rescore(old, doc):
    """A saved report's answers scored again with the current scorer and case file (no
    model call), so a baseline and a later run are judged by the same scorer."""
    cases = {c["id"]: c for c in doc["cases"]}
    rows = []
    for r in old["cases"]:
        case = cases[r["id"]]
        keep = {k: r[k] for k in ("http", "status", "reason_code", "answer", "status_line",
                                  "citations", "latency_s") if k in r}
        row = dict(case, **keep)
        row["score"] = score(case, row["status"], row["answer"], doc)
        row["answer_sha256"] = answer_sha256(row["status"], row["answer"])
        rows.append(row)
    report = {k: old[k] for k in ("generated", "today", "model", "prompt_template", "aliases", "dev")
              if k in old}
    report.update(cases=rows, by_type=by_type(rows), rescored=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    adjudicate(report)
    return report


def compare(old, new):
    """Lines naming every case whose status or answer changed between two reports."""
    out = []
    for key in ("cases", "dev"):
        before = {r["id"]: r for r in old.get(key, [])}
        for r in new.get(key, []):
            b = before.get(r["id"])
            if b is None:
                out.append("%s %s: new" % (key, r["id"]))
            elif (b["status"], b["answer"]) != (r["status"], r["answer"]):
                out.append("%s %s: %s -> %s" % (key, r["id"], b["status"], r["status"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=None)
    ap.add_argument("--stub-model", action="store_true", help="pipeline only; no weights")
    ap.add_argument("--label", required=True)
    ap.add_argument("--dev", action="store_true", help="also the 10 dev-case questions")
    ap.add_argument("--compare", default=None, help="an earlier report to compare with")
    ap.add_argument("--cases", default=None)
    ap.add_argument("--rescore", default=None, help="score a saved report's answers again")
    ap.add_argument("--aliases", action="store_true",
                    help="P2.5 F3: expand questions with the case file's synthetic alias table")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.label):
        raise SystemExit("label: letters, digits, dot, dash, underscore only")
    if args.rescore:
        old = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        return _finish(args, rescore(old, load_cases(args.cases)))

    if not (args.stub_model or args.endpoint):
        # Never the live phone server by default (port 18087): name the evaluation server.
        raise SystemExit("name the evaluation server with --endpoint, or use --stub-model")
    model = (adapter_mod.StubAdapter() if args.stub_model
             else adapter_mod.LlamaCppAdapter(endpoint=args.endpoint))
    dev_cases = None
    if args.dev:
        import run_dev_cases  # the reserved acceptance cases stay refused there
        dev_cases = run_dev_cases.load_dev_cases()
    report = run(load_cases(args.cases), model, dev=args.dev, dev_cases=dev_cases,
                 use_aliases=args.aliases)
    return _finish(args, report)


def _finish(args, report):
    out = config.ARTIFACTS / "p25" / ("%s.json" % args.label)
    try:
        outpath.assert_outside_vault(out)
    except outpath.OutsideVaultError as exc:
        raise SystemExit("REFUSED: %s" % exc)
    with outpath.open_for_write(out) as fh:
        fh.write(json.dumps(report, indent=1, sort_keys=True, ensure_ascii=False))

    for r in report["cases"]:
        s = r["score"]
        print("%-32s %-8s %-10s %-9s %-6s %s" % (r["id"], r["type"], r["status"], s["outcome"],
                                                r.get("read", {}).get("verdict", "-"),
                                                "; ".join(s["reasons"])))
    print("by type: %s" % "  ".join("%s %d/%d (fail %d, unscored %d)" % (
        t, v["pass"], v["cases"], v["fail"], v["unscored"]) for t, v in report["by_type"].items()))
    if report["needs_read"]:
        print("needs a read: %s" % ", ".join(report["needs_read"]))
    if args.compare:
        changed = compare(json.loads(Path(args.compare).read_text(encoding="utf-8")), report)
        print("changed since %s: %d" % (args.compare, len(changed)))
        for line in changed:
            print("  " + line)
    print("report: %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
