"""Turn a question into a cited answer, an incomplete answer, or an abstention.

`supported` is deliberately unreachable unless a verifier is supplied.

The lexical grounding check catches an answer that cites real evidence while talking
about something else, but it cannot tell a true claim from a plausible false one built
from the evidence's own vocabulary: "tempo means marathon pace or slower" scores 80%
against evidence saying "or faster". Documenting that limitation is not enough - a
verdict of `supported` next to a contradiction is simply wrong. So grounding is treated
as a *necessary* condition, and the sufficient one is an entailment check this module
does not implement. With no verifier, the best available verdict is `incomplete`.

Supplying a verifier is T4.1 / P2.1 work. Until then the service never claims support
it cannot demonstrate.
"""
import re
import unicodedata

from . import adapter as adapter_mod
from . import config, index, retrieve
from .retrieve import terms

CITE = re.compile(r"\[(E\d+)\]")

SUPPORTED, INCOMPLETE, WITHHELD = "supported", "incomplete", "withheld"
# Trace-only: a query that got no answer from the model. Never shown as a verdict; the
# CLI fails loudly instead.
FAILED = "failed"

# Why a verdict was given, as a fixed code next to the readable `reason` (P2.7 step C1b,
# D113). `incomplete` covers two different things until a verifier exists: an answer that
# cited real evidence and stays on it but whose entailment nobody checked (UNVERIFIED),
# and a real defect. Only the defects, with the other failure signals, may store a
# question (P2.7 step C2); UNVERIFIED is counted, never stored. The status and the text
# the phone shows do not change.
NO_EVIDENCE = "no_evidence"              # withheld before the model: nothing retrieved
DECLINED = "declined"                    # withheld: the model said the notes don't say
INVALID_CITATION = "invalid_citation"    # incomplete: cited an id never offered
NO_CITATION = "no_citation"              # incomplete: answered without citing
LOW_GROUNDING = "low_grounding"          # incomplete: wording strays from the evidence
UNVERIFIED = "unverified"                # incomplete: cited and grounded, no verifier
VERIFIER_REJECTED = "verifier_rejected"  # incomplete: a verifier said unsupported
VERIFIED = "verified"                    # supported
MODEL_UNAVAILABLE = "model_unavailable"  # failed (trace only): no reply from the model
NEGATION_VS_DATED = "negation_vs_dated_evidence"  # incomplete: said no, a cited line lists an event that day
DEFECTS = frozenset({INVALID_CITATION, NO_CITATION, LOW_GROUNDING, VERIFIER_REJECTED,
                     NEGATION_VS_DATED})

PROMPT = """You answer only from the evidence below, about David's own notes.

Rules:
- Use only the evidence. If it does not answer the question, say so plainly.
- Cite the evidence you used by its bracketed id, like [E1]. Cite nothing else.
- Do not guess, and do not use knowledge from outside the evidence.

Question: %s

Evidence:
%s

Answer (with [En] citations):"""


# Phrases with which a model declines. If it says it cannot answer, the service must not
# dress that up as an answer just because a citation is attached.
DECLINE_PHRASES = (
    "insufficient evidence", "no evidence", "not enough evidence", "cannot answer",
    "can't answer", "unable to answer", "does not say", "doesn't say", "not stated",
    "not specified", "no information", "i don't know", "i do not know",
    "the evidence does not", "nothing in the evidence", "remains unknown",
    "is unknown", "not known", "no mention", "does not mention", "doesn't mention",
)

# Fraction of the answer's content words that must also appear in the cited evidence.
#
# Set deliberately low. This is a *grounding* check, not entailment verification: it
# catches an answer that cites real evidence while talking about something else
# entirely - the "moon is made of cheese [E1]" failure, which scores 0.
#
# Kept low on purpose. Measured overlap for a *correct* short answer against the real
# corpus was 0.20, because a two-line chunk shares little vocabulary with a one-sentence
# paraphrase. A higher bar would reject good answers while still not catching
# contradictions, which is why this is only a necessary condition and never a sufficient
# one. Nothing here is faithfulness checking: that is T4.1 / P2.1 work.
GROUNDING_THRESHOLD = 0.15


def _declines(text):
    low = text.lower()
    return any(phrase in low for phrase in DECLINE_PHRASES)


# P2.5 F2 (part of layer 7, D120): the phone said "nothing on that day" while the note it
# cited lists an event on that day (7 and 9 Oct). A deterministic check, English and French
# only (D121): pure, no I/O, no model call. Narrow by design (D74 consult and David, 13 Oct,
# session 47): free text cannot be read safely, so it fires only when the whole answer is a
# bare "nothing on <the asked day>" and a chunk the answer itself cites holds a clean table
# row for that day. Everything else is left alone; a miss is safe, a wrong fire is not, and
# when it fires the phone keeps the model's answer and adds the row (serve.py).
_F2_MONTHS = (
    ("jan", "january", "janvier", "janv"), ("feb", "february", "février", "fevrier", "févr", "fevr"),
    ("mar", "march", "mars"), ("apr", "april", "avril", "avr"), ("may", "mai"),
    ("jun", "june", "juin"), ("jul", "july", "juillet", "juil"),
    ("aug", "august", "août", "aout"), ("sep", "sept", "september", "septembre"),
    ("oct", "october", "octobre"), ("nov", "november", "novembre"),
    ("dec", "december", "décembre", "decembre", "déc"))
_F2_WEEKDAYS = (("mon", "monday", "lun", "lundi"), ("tue", "tues", "tuesday", "mar", "mardi"),
                ("wed", "wednesday", "mer", "mercredi"),
                ("thu", "thur", "thurs", "thursday", "jeu", "jeudi"),
                ("fri", "friday", "ven", "vendredi"), ("sat", "saturday", "sam", "samedi"),
                ("sun", "sunday", "dim", "dimanche"))


def _f2_alt(words):
    return "|".join(sorted(map(re.escape, words), key=len, reverse=True))


# Any four-digit number may be a year (1885, 2105: F2 D74 consult, 13 Oct).
_F2_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")
# Words that make a row or the chunk's prose doubtful: negation, cancellation, a change, a
# deadline, a template, "to be confirmed". Any of them and the check stays silent.
_F2_DOUBT = re.compile(
    r"n't\b|\bn'\s*\w|\b(?:no|not|none|nothing|never|cannot|unable|cancel+ed|cancel|closed|"
    r"off|postponed|moved|replaced|deadline|due|book|booking|register|registration|sign-up|"
    r"template|example|tbc|tbd|maybe|optional|cancellations?|suspended|holidays?|vacation|"
    r"break|free|vacant|nil|n/a|updated|created|modified|last|"
    r"non|pas|plus|aucun|aucune|rien|sans|jamais|néant|annulations?|suspendu[e]?s?|vacances|congés?|"
    r"férié[e]?s?|libre|mis à jour|créé[e]?|modifié[e]?|révisé[e]?|dernière?|"
    r"annul[ée]e?s?|ferm[ée]e?s?|relâche|report[ée]e?s?|déplac[ée]e?s?|supprim[ée]e?s?|"
    r"remplac[ée]e?s?|échéance|limite|inscriptions?|réservations?|exemple|modèle|"
    r"facultatif|facultative|peut-être|withdrawn|abolished|scrapped|dropped|aborted|void|"
    r"deferred|ajourn\w*|rescinded|scrubbed|résili\w*|axed|décommand\w*|"
    r"déprogramm\w*|retiré[e]?s?|abandonn\w*)\b|\bto be confirmed\b|à confirmer|\?|~~|"
    r"<(?:del|s|strike)\b", re.I)
# Numbers in words (a year written out, "the eleventh"), EN and FR.
_F2_NUMBER_WORDS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\w+teen|\w+teenth|"
    r"twenty|thirty|twentieth|thirtieth|hundred|thousand|first|second|third|fourth|fifth|"
    r"sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|deux|trois|quatre|cinq|sept|huit|"
    r"neuf|dix|onze|douze|treize|quatorze|quinze|seize|vingt|trente|cent|cents|mille|"
    r"premier|première|\w+ième")
_F2_NUMBER = re.compile(r"\b(?:%s)\b" % _F2_NUMBER_WORDS, re.I)
# Words in an event cell that may name the event's own day, so the row's date cell may not
# be it: a weekday or month, a relative day, a number in words (F2 reviews of session 48,
# 13 Oct: "Rehearsal on Sunday the eleventh of October", "the day after", "surlendemain").
_F2_OTHER_DAY = re.compile(
    r"\b(?:%s|today|tonight|tomorrow|yesterday|next|following|previous|week|weekend|after|"
    r"before|later|earlier|eve|aujourd'hui|demain|hier|lendemain|surlendemain|veille|"
    r"avant-veille|après|avant|plus tard|suivante?s?|prochaine?s?|précédente?s?|semaine|"
    r"week-end|fortnight|quinzaine|%s)\b"
    % (_f2_alt({w for n in _F2_MONTHS + _F2_WEEKDAYS for w in n}), _F2_NUMBER_WORDS), re.I)
# What an event cell may hold once its times are removed: letters and plain punctuation.
# Anything else (a symbol such as ❌ or ✓, a digit) and the check stays silent. A search for
# one forbidden character, so its cost stays linear in the cell's length.
_F2_NOT_PLAIN = re.compile(r"[^\w\s'\-–—,.;:()&/+]|[\d_]")
# The header of the date's column: the date must be the row's date, not a logging date.
_F2_DATE_HEADER = re.compile(r"dates?|days?|when|jours?|quand", re.I)


# A time: 10:00, 10h, 10h00. Not "10.10", which may be a date (F2 review 1, 13 Oct).
_F2_TIME = re.compile(r"(?<![\d/.:])\d{1,2}(?::\d{2}|h(?:\d{2})?)(?![\d/.:])", re.I)


_F2_ISO = re.compile(r"(?<!\d)(\d{4})-\d{2}-\d{2}(?!\d)")
_F2_ANY_MONTH = _f2_alt({w for m in _F2_MONTHS for w in m})
_F2_ANY_WEEKDAY = _f2_alt({w for n in _F2_WEEKDAYS for w in n})
_F2_WORD_DATE = re.compile(
    r"(?:(?:%(wd)s)\.?,?\s+)?(?:le\s+)?(?<!\d)(?P<d1>\d{1,2})(?:er)?\.?\s+(?:%(mo)s)\b\.?"
    r"(?:,?\s+(?P<y1>\d{4}))?(?!\d)"
    r"|(?:(?:%(wd)s)\.?,?\s+)?(?:%(mo)s)\b\.?\s+(?<!\d)(?P<d2>\d{1,2})(?:,?\s+(?P<y2>\d{4}))?(?!\d)"
    % {"wd": _F2_ANY_WEEKDAY, "mo": _F2_ANY_MONTH}, re.I)


def _f2_numbers_accounted(text, year):
    """Every number in a cited chunk is one F2 understands, or the check stays silent: an
    ISO date or a written date (EN/FR) in the asked year or with no year, the asked year
    alone, or a time. Allowed, not forbidden, so an unforeseen spelling of another day or
    year ("11 / 10 / 20", "Sun 11 Oct 20", "11⁄10⁄20") cannot slip through; any numeric
    character other than ASCII 0-9 ("¹¹", "₂₀", "²⁰²⁰") also silences it (F2 D74 consult,
    15 Oct, session 49)."""
    if any(unicodedata.category(ch)[0] == "N" and ch not in "0123456789" for ch in text):
        return False

    def iso(m):
        return " " if int(m.group(1)) == year else "0"  # a digit left over: silent

    def word_date(m):
        d, y = m.group("d1") or m.group("d2"), m.group("y1") or m.group("y2")
        return " " if 1 <= int(d) <= 31 and (y is None or int(y) == year) else "0"

    text = _F2_ISO.sub(iso, text)
    text = _F2_TIME.sub(" ", text)
    text = _F2_WORD_DATE.sub(word_date, text)
    text = re.sub(r"(?<!\d)%d(?!\d)" % year, " ", text)
    return not re.search(r"\d", text)


def _f2_norm(text):
    text = (text or "").replace("’", "'").replace("‘", "'").replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def _f2_day(day, weekday_needed=False):
    """A regex for `day` written as a date alone: an optional weekday (only `day`'s own),
    the day and month (EN or FR, either order), an optional year (only `day`'s own), or
    ISO. With `weekday_needed`, the weekday or the year must be there."""
    wd = _f2_alt(_F2_WEEKDAYS[day.weekday()])
    mo = _f2_alt(_F2_MONTHS[day.month - 1])
    d, y = r"0?%d" % day.day, r"%d" % day.year
    lead = r"(?:%s)\.?,?\s+" % wd

    def forms(w, yr):  # day-month and month-day, with weekday part `w` and year part `yr`
        return [r"%s(?:le\s+)?%s(?:er)?\.?\s+(?:%s)\.?%s" % (w, d, mo, yr % r"\s+"),
                r"%s(?:%s)\.?\s+%s%s" % (w, mo, d, yr % r",?\s+")]

    iso = r"%d-%02d-%02d" % (day.year, day.month, day.day)
    if not weekday_needed:
        return "(?:%s)" % "|".join(forms("(?:%s)?" % lead, "(?:%%s%s)?" % y) + [iso])
    # Each form spells the weekday or the year as a part of its own, not by a lookahead:
    # "mar" is both March and mardi (F2 D74 consult, 13 Oct).
    return "(?:%s)" % "|".join(forms(lead, "(?:%%s%s)?" % y)
                               + forms("(?:%s)?" % lead, "%%s%s" % y) + [iso])


def _f2_bare_denial(text, day):
    """The whole answer, citations aside, is one "nothing on <day>" sentence."""
    date = _f2_day(day)
    body = (r"(?:there\s+is\s+)?nothing\s+(?:is\s+)?(?:found|scheduled|planned|listed)\s+"
            r"(?:for|on)\s+{d}"
            r"|there\s+are\s+no\s+(?:events|entries)\s+(?:(?:scheduled|planned|listed)\s+)?"
            r"(?:for|on)\s+{d}"
            r"|(?:il\s+n'y\s+a\s+)?rien\s+(?:de\s+prévu|de\s+programmé|n'est\s+prévu|trouvé|"
            r"n'a\s+été\s+trouvé)\s+(?:pour\s+le|pour|le)\s+{d}"
            r"|aucun\s+événement\s+(?:n'est\s+)?(?:prévu\s+)?(?:pour\s+le|le)\s+{d}").format(d=date)
    whole = (r"(?:{d}\s*:\s*)?(?:(?:no|non)\s*[,.]\s*)?(?:{b})"
             r"(?:\s+(?:in\s+the\s+(?:notes|evidence)|dans\s+les\s+notes))?\s*\.?").format(d=date, b=body)
    return re.fullmatch(whole, _f2_norm(CITE.sub(" ", text or "")), re.I) is not None


def _f2_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _f2_row(line, header, cell_date):
    """A clean table row for the day: one cell is the date alone (with its weekday or its
    year), under a Date/Day/Jour column of the table's `header`; every other cell is plain
    words (times aside) naming no other day, and one holds an event word; nothing doubtful."""
    if _F2_DOUBT.search(line):
        return False
    cells = _f2_cells(line)
    dated = [i for i, c in enumerate(cells) if cell_date.fullmatch(c)]
    if len(dated) != 1 or dated[0] >= len(header) \
            or not _F2_DATE_HEADER.fullmatch(header[dated[0]]):
        return False
    rest = [_F2_TIME.sub(" ", c).strip() for i, c in enumerate(cells) if i != dated[0]]
    # A digit, a symbol, or a word that may name another day ("11/10", "❌", "Sunday the
    # eleventh") could mean the event is not on the row's date: stay silent (F2 reviews).
    if any(c and (_F2_NOT_PLAIN.search(c) or _F2_OTHER_DAY.search(c)) for c in rest):
        return False
    return any(re.search(r"[^\W\d_]{3,}", c) for c in rest)


def dated_contradiction(answer_text, cited, days):
    """The first cited table row for the asked day that a bare "nothing on that day" answer
    contradicts, as (label, row); else None.

    `cited`: (label, chunk text) for each chunk the answer cites; `days`: the dates the
    question was resolved to (exactly one, or the check stays silent). A chunk counts only
    if the asked year appears in it and no other (a chunk split off under a heading may have
    lost its note's year: F2 D74 consult, 13 Oct), and none of its lines outside the table
    rows (prose and header rows) is doubtful (`_F2_DOUBT`) or holds a digit other than the
    asked year or a time ("'20": F2 review 1 of session 48)."""
    if len(days) != 1 or not _f2_bare_denial(answer_text, days[0]):
        return None
    day = days[0]
    cell_date = re.compile(_f2_day(day, weekday_needed=True), re.I)
    own_year = re.compile(r"(?<!\d)%d(?!\d)" % day.year)
    for label, chunk in cited:
        lines = [_f2_norm(raw) for raw in (chunk or "").splitlines() if raw.strip()]
        years = {int(y) for y in _F2_YEAR.findall(" ".join(lines))}
        # A two-digit year anywhere, in prose or any row ("October '20", "11/10/20"), silences
        # it too (F2 reviews 3 and 4 of session 48).
        if years != {day.year} or re.search(r"'\d{2}(?!\d)|(?<!\d)\d{1,2}[/.-]\d{1,2}[/.-]\d{2}(?!\d)",
                                            " ".join(lines)):
            continue
        if not _f2_numbers_accounted("\n".join(lines), day.year):
            continue
        # Tables: a header row, its separator, then body rows. Rows under no header are not
        # read as rows; they count as prose.
        rows, prose, header = [], [], None
        for i, line in enumerate(lines):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if line.startswith("|") and re.fullmatch(r"\|[\s:|-]+\|?", nxt):
                header = _f2_cells(line)
                prose.append(line)
            elif line.startswith("|") and re.fullmatch(r"\|[\s:|-]+\|?", line):
                continue
            elif line.startswith("|") and header is not None:
                rows.append((line, header))
            else:
                header = None
                prose.append(line)
        # A year may be written in words ("two thousand twenty": F2 review 2 of session 48).
        if any(_F2_DOUBT.search(x) or _F2_NUMBER.search(x)
               or re.search(r"\d", _F2_TIME.sub(" ", own_year.sub(" ", x))) for x in prose):
            continue
        for line, head in rows:
            if _f2_row(line, head, cell_date):
                return label, line
    return None


def grounding_overlap(answer_text, cited_chunks):
    """Share of the answer's content words that appear in the evidence it cited."""
    stripped = CITE.sub(" ", answer_text)
    words = terms(stripped)
    if not words:
        return 0.0
    evidence = set()
    for chunk in cited_chunks:
        evidence.update(terms(chunk.heading + " " + chunk.text))
    if not evidence:
        return 0.0
    hit = sum(1 for w in words if w in evidence)
    return hit / float(len(words))


class Result:
    def __init__(self, question, status, answer, citations, hits, timing=None, invalid=(),
                 files=None, grounding=None, reason=None, skipped=None, reason_code=None):
        self.question = question
        self.status = status
        self.answer = answer
        self.citations = citations      # vault paths with line ranges
        self.hits = hits
        self.timing = timing or {}
        self.invalid = tuple(invalid)
        self.files = files or {}          # every note indexed for this query
        self.grounding = grounding        # overlap score, or None if not computed
        self.reason = reason              # why this verdict, in one phrase
        self.reason_code = reason_code    # the same, as one of the codes above
        self.skipped = list(skipped or [])  # (path, reason): read, refused by the label rules, never indexed

    def render(self):
        header = "[%s]" % self.status.upper()
        if self.reason:
            header += " (%s)" % self.reason
        lines = ["%s %s" % (header, self.answer.strip())]
        if self.citations:
            lines.append("")
            lines.append("Sources:")
            for c in self.citations:
                lines.append("  - %s" % c)
        if self.invalid:
            lines.append("")
            lines.append(
                "Ignored invented citations: %s" % ", ".join(self.invalid)
            )
        return "\n".join(lines)


def ask(question, profile=None, model=None, vault=None, limit=4, verifier=None, prompt=None):
    """Answer `question` from the profile's slice of the vault.

    `verifier`, when given, is called as verifier(answer_text, cited_chunks) -> bool and
    decides entailment. Without one, no answer is reported as `supported`.

    `prompt` replaces PROMPT for one caller (the MVP's fact-first wording) without
    changing the prompt version the P2.2 measurements are pinned to.
    """
    profile = profile or config.DEFAULT
    skipped = []
    chunks, files = index.build(profile, vault, skipped=skipped)
    ix = retrieve.Index(chunks)
    hits = ix.search(question, limit=limit)

    if not hits:
        # Abstain before calling the model at all: there is nothing to ground an answer in.
        return Result(
            question,
            WITHHELD,
            "No supporting evidence in the %s corpus, so I am not answering. "
            "Widen the allowlist or ask about something the notes cover." % profile.name,
            [],
            [],
            files=files, skipped=skipped,
            reason="no evidence retrieved", reason_code=NO_EVIDENCE,
        )

    model = model or adapter_mod.LlamaCppAdapter()
    # Label the evidence E1..En for this query only. Global chunk ids are sparse once
    # retrieval has filtered them, and a model asked to cite [C18] out of four options
    # is being invited to guess.
    labelled = [("E%d" % i, c) for i, (_, c) in enumerate(hits, 1)]
    evidence = "\n\n".join(
        "[%s] %s (%s)\n%s" % (label, c.heading or "(no heading)", c.cite(), c.text)
        for label, c in labelled
    )
    try:
        text, elapsed = model.generate((prompt or PROMPT) % (question, evidence))
    except adapter_mod.ModelUnavailable as exc:
        # Round D finding 3: the corpus was read and the model was called, so this query
        # still owes a trace (condition 6). Attach what is known; the caller writes it.
        # Round E finding 4: the exception text can quote the model server's error object,
        # a redirect URL or a finish_reason, any of which can echo the question. The
        # trace gets the exception class only, never remote text.
        exc.partial = Result(
            question, FAILED, "", [], hits, {}, (), files=files, skipped=skipped,
            reason=type(exc).__name__, reason_code=MODEL_UNAVAILABLE,
        )
        raise

    by_id = dict(labelled)
    cited = []
    invalid = []
    for cid in CITE.findall(text):
        if cid in by_id and cid not in cited:
            cited.append(cid)
        elif cid not in by_id and cid not in invalid:
            invalid.append(cid)

    # Four ways to fail to be "supported", checked in order of severity.
    cited_chunks = [by_id[c] for c in cited]
    overlap = grounding_overlap(text, cited_chunks) if cited_chunks else 0.0

    if _declines(text):
        # The model itself said it could not answer. Report that, not a false positive.
        status, code, reason = WITHHELD, DECLINED, "the model declined on the available evidence"
    elif invalid:
        status, code, reason = INCOMPLETE, INVALID_CITATION, (
            "cited an id that was never offered: %s" % ", ".join(invalid))
    elif not cited:
        status, code, reason = INCOMPLETE, NO_CITATION, "answered without citing any evidence"
    elif overlap < GROUNDING_THRESHOLD:
        # Cited something real, but the answer is largely not about it.
        status, code, reason = INCOMPLETE, LOW_GROUNDING, (
            "only %.0f%% of the answer's wording appears in the evidence it cited "
            "(threshold %.0f%%)" % (overlap * 100, GROUNDING_THRESHOLD * 100)
        )
    elif verifier is None:
        # Grounding is necessary but not sufficient. No entailment check is available,
        # so support is not claimed. See the module docstring.
        status, code, reason = INCOMPLETE, UNVERIFIED, (
            "grounded (%.0f%% lexical overlap) but entailment unverified: no verifier "
            "is available, so support is not claimed" % (overlap * 100)
        )
    elif not verifier(text, cited_chunks):
        status, code, reason = INCOMPLETE, VERIFIER_REJECTED, (
            "a verifier rejected the answer as unsupported")
    else:
        status, code, reason = SUPPORTED, VERIFIED, (
            "cited evidence, %.0f%% lexical overlap, entailment confirmed" % (overlap * 100)
        )

    return Result(
        question,
        status,
        text,
        [c.cite() for c in cited_chunks],
        hits,
        {"model_seconds": elapsed},
        invalid,
        files=files, skipped=skipped,
        grounding=round(overlap, 3),
        reason=reason, reason_code=code,
    )
