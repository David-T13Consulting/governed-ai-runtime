"""Leak check for synthetic equivalents (P2.7, decision 4, option B).

A local model rewrites each failed question into a *synthetic equivalent* that cloud agents
may read. That rewrite is not trusted: this deterministic check compares it with the
original and flags it if anything personal may have survived. A flagged item goes to David
and reaches no cloud agent until he decides. The check errs on the side of flagging: a
flagged item costs David one look; a missed leak sends personal text to the cloud.

`check(original, synthetic)` is a pure function: no I/O, no model call, and the result
holds rule numbers only, never the words that matched, so it can go to the audit log.

The rules (fixed in the P2.7 brief, Codex-reviewed; changing one is a reviewed change):
  1. a word capitalised anywhere in the original (sentence openers included) that appears
     in the synthetic;
  2. a digit sequence of the original that appears in the synthetic;
  3. date or time text in the synthetic: anything the service's date reader finds, resolved
     or not; any month, weekday, relative-day, time-of-day or day-number word (EN, FR, NL);
     and any clock time however written (14:30, 14h, 2pm, 9 uur);
  4. a shared word, of any length, that is not on the function-word list below;
  5. an e-mail address, URL (any scheme, www., or a domain-like token) or phone-number-like
     digit run in the synthetic, after NFKC (fullwidth forms included);
  6. the synthetic shares 60 % or more of the original's word set (Jaccard); also any input
     that cannot be compared (not text, or no words on either side).
Comparison is after Unicode NFKC, lower-casing and accent folding; words are runs of
letters or digits.
"""
import re
import unicodedata
from collections import namedtuple
from datetime import date

Result = namedtuple("Result", "flagged rules_hit")

NEAR_COPY = 0.60

# Rule 4's exclusions: function words only (articles, pronouns, prepositions, conjunctions,
# auxiliaries, question words, negation) in English, French and Dutch, accent-folded. No
# adverb, quantifier, domain or activity word belongs here: "just", "very", "some",
# "climbing", "school", "menu" must stay uncommon.
FUNCTION_WORDS = frozenset("""
a an the this that these those it its i me my mine we us our you your he him his she her
they them their there what which who whom whose when where why how whether if than and or
but nor not no do does did be is am are was were been being have has had having will would
shall should can could may might must of in on at to for from by with about into onto over
under up down out off as
le la les l un une des du de d au aux ce cet cette ces c ca je j me m tu te t il elle on
nous vous ils elles se s y en mon ma mes ton ta tes son sa ses notre nos votre vos leur
leurs qui que qu quoi dont ou est sont suis es etes sommes etait ete avoir ai as avons
avez ont a et mais donc or ni car pour par avec sans sous sur dans chez vers entre
pas ne n quel quelle quels quelles quand comment pourquoi combien si
de het een dit dat deze die er ik mij me mijn wij we ons onze jij je jou jouw u uw hij
hem zijn zij ze haar hun wat welk welke wie wanneer waar waarom hoe of als dan en maar
want dus niet geen ben bent is was waren heb hebt heeft hebben had hadden zal
zult zullen kan kunt kunnen moet moeten mag mogen wil willen van in op aan te voor met
door over onder naar uit bij tot om tegen zonder
""".split())

# Rule 3's date words (accent-folded): months, weekdays, relative days, day numbers.
DATE_WORDS = frozenset("""
january february march april may june july august september october november december
jan feb mar apr jun jul aug sep sept oct nov dec
janvier fevrier mars avril mai juin juillet aout septembre octobre novembre decembre
januari februari maart mei juni juli augustus oktober
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
lundi mardi mercredi jeudi vendredi samedi dimanche
maandag dinsdag woensdag donderdag vrijdag zaterdag zondag
today tomorrow yesterday tonight weekend week month year
aujourd hui demain hier soir semaine mois annee lendemain veille surlendemain
vandaag morgen gisteren overmorgen eergisteren vanavond vannacht vanochtend vanmorgen vanmiddag
morgenochtend morgenmiddag morgenavond gisterochtend gistermiddag gisteravond gisternacht
weekend week maand jaar ochtend middag avond nacht
morning afternoon evening night noon midnight matin midi apres nuit soiree minuit middernacht
oclock clock hour hours heure heures uur half quarter quart kwart demi
dawn dusk sunrise sunset daybreak aube aurore crepuscule ochtendgloren dageraad schemering
zonsopgang zonsondergang
spring summer autumn fall winter printemps ete automne hiver lente zomer herfst
first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth
thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth twentieth
thirtieth one two three four five six seven eight nine ten eleven twelve thirteen
fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty
premier un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze
quinze seize vingt trente
een twee drie vier vijf zes zeven acht negen tien elf twaalf dertien veertien vijftien
zestien zeventien achttien negentien twintig dertig eerste tweede derde
""".split()) - frozenset([
    # Removed because they are mainly something else: French "mon" (my), the articles
    # "un" and "een", "one", and "second" ("second brain"). Everything else stays.
    "mon", "un", "een", "one", "second"])

_WORD = re.compile(r"[^\W_]+")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_URL = re.compile(
    r"\b[a-z][a-z0-9+.\-]*:[^\s:]\S*"           # any scheme, with or without //: file:/x, data:x
    r"|\bwww\.\S+"
    # a domain-like token in any script, combining marks included in every label: one or more
    # labels and a dot, then an ending that starts with a letter and has two or more characters
    # (example.dev, example.测试, संस्था.भारत); "e.g" is not one
    r"|(?:[^\s.,;:!?()\[\]{}\"'<>/\\@]+\.)+[^\W\d_][^\s.,;:!?()\[\]{}\"'<>/\\]+",
    re.I)
_PHONE = re.compile(r"(?<!\w)\+?\d(?:[\s.\-/\u2010-\u2015\u2212]*\d){6,}(?!\w)")
_DIGITS = re.compile(r"\d+")
# Rule 3 also counts a time, however it is written: 14:30, 14h30, 14h, 2pm, 2 pm, 9u, 9 uur.
_TIME = re.compile(r"\b\d{1,2}\s*(?::\s*\d{2}|h\s*\d{0,2}|am\b|pm\b|u\b|uur\b|heures?\b|o.?clock\b)", re.I)


# Lookalikes folded before any matching: dot-like separators (IDNA treats them as dots) become
# ".", every dash becomes "-", and apostrophe-like marks become a space ("o'clock" -> "o clock").
_LOOKALIKES = str.maketrans({**{c: "." for c in "\u3002\uff61\uff0e"},
                             **{c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2e3a\u2e3b\u2212\ufe58\ufe63\uff0d"},
                             **{c: " " for c in "'\u2018\u2019\u02bc\u02bb\u00b4\u0060\uff07"}})


def _norm(text):
    """NFKC, then lookalike separators made plain; every dash (Unicode category Pd) is "-"."""
    text = unicodedata.normalize("NFKC", text or "").translate(_LOOKALIKES)
    return "".join("-" if unicodedata.category(ch) == "Pd" else ch for ch in text)


def fold(text):
    """NFKC, lookalikes, lower case, accents removed: 'Léo' and 'leo' compare equal."""
    text = _norm(text)
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def words(text):
    return _WORD.findall(fold(text))


def _strip_accents(text):
    """NFKC, then accents removed, case kept: rule 1 must see "W\u030aill" as one word."""
    text = unicodedata.normalize("NFD", _norm(text))
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _capitalised(text):
    """Rule 1's words: every word capitalised in the original, sentence openers included
    (a sentence-opening "Will" may be a name)."""
    return {fold(w) for w in _WORD.findall(_strip_accents(text)) if w[:1].isupper()}


# Dutch writes times of day as one word ("morgenavond", "gisteravond"): any word starting
# with these is date text too.
_DATE_PREFIXES = ("morgen", "gister", "vanoch", "vanmid", "vanav", "vannacht", "vanmorgen",
                  "overmorgen", "eergister")


def _dates_found(text):
    """Rule 3 via the service's own date reader, resolved or not, on the NFKC text and on
    its folded form. Imported late so the check stays usable without the service; a reader
    error flags (fails closed)."""
    try:
        from dbrain.serve import resolve_dates
        nfkc = unicodedata.normalize("NFKC", text)
        return any(resolve_dates(t, date(2026, 1, 1))[1] for t in (nfkc, nfkc.lower(), fold(text)))
    except Exception:
        return True


def check(original, synthetic):
    """Compare a synthetic equivalent with its original. Rule numbers only, sorted.
    Anything that is not a pair of strings with words on both sides cannot be shown safe:
    it is flagged (rule 6)."""
    if not isinstance(original, str) or not isinstance(synthetic, str):
        return Result(True, (6,))
    hits = set()
    o_words, s_words = words(original), words(synthetic)
    if not o_words:          # no original to compare with: nothing proves the rewrite safe
        hits.add(6)
    o_set, s_set = set(o_words), set(s_words)

    if _capitalised(original) & s_set:
        hits.add(1)
    if set(_DIGITS.findall(fold(original))) & set(_DIGITS.findall(fold(synthetic))):
        hits.add(2)
    if (s_set & DATE_WORDS) or any(w.startswith(_DATE_PREFIXES) for w in s_set) \
            or _TIME.search(fold(synthetic)) or _dates_found(synthetic):
        hits.add(3)
    if (o_set & s_set) - FUNCTION_WORDS:
        hits.add(4)
    s_nfkc = _norm(synthetic)
    if _EMAIL.search(s_nfkc) or _URL.search(s_nfkc) or _PHONE.search(s_nfkc):
        hits.add(5)
    union = o_set | s_set
    if union and len(o_set & s_set) / len(union) >= NEAR_COPY:
        hits.add(6)
    if not s_words:          # an empty rewrite is not a rewrite: David looks at it
        hits.add(6)
    return Result(bool(hits), tuple(sorted(hits)))
