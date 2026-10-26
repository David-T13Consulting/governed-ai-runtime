"""P2.5 F3 (layer 4, minimal): David's words for things his notes name otherwise.

A question that says "cantata" retrieves nothing from a note that only says "choir". Before
retrieval, the phone appends each matched alias's note wording to the question, so the
lexical index can find the note. Read-only: a module constant, never written at run time.

Generic terms only (data policy §3a): no names of people, no places, nothing that identifies
anyone. A new entry is a code change, reviewed like any other.
"""
import re
import unicodedata

# David's word (singular and plural forms as written) -> the words his notes use.
ALIASES = {
    ("cantata", "cantatas"): ("choir", "rehearsal"),  # example seed (the real table is local)
}

_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.U)


def _fold(word):
    """Case- and accent-insensitive form: "Cantata" and "cantáta" both read "cantata"."""
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def expand(question, table=None):
    """`question` with the note wording of every alias it uses appended once, in table
    order, as "(also: choir, rehearsal)". The question itself is never changed; with no
    match it comes back as it was."""
    table = ALIASES if table is None else table
    # Fold first, then split: a decomposed accent ("i" + U+0301) would otherwise split the
    # word (F3 review 1, should-fix). The question itself is returned unchanged.
    words = set(_WORD.findall(_fold(question or "")))
    extra = []
    for keys, terms in table.items():
        if words & {_fold(k) for k in keys}:
            extra += [t for t in terms if _fold(t) not in words and t not in extra]
    return "%s (also: %s)" % (question, ", ".join(extra)) if extra else question
