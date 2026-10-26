"""PL1 step 6 (P3.0 Part 1): "remember from the phone", the drafting side. Pure functions,
no I/O: `check` turns what David dictated, and his "work or personal?" answer, into either
a refusal or a draft; `render` turns a draft into the exact bytes of one new Inbox note.
The writer (`writer.py`) is the only code that puts those bytes in the vault. Step 7's
corrections (D143) are the same kind of note, rendered by `render_correction`.

**The gate is David's read-back and "save" (Q154 (a), David 22 Oct).** The service speaks
the draft back and writes it only when he says "save". The word lists below are a second
line that fails closed: they refuse what they detect and never approve anything. They
cannot see implicit meaning ("they accepted my day rate"), unknown names or spelled-out
tokens; that residual risk, content David himself approves on read-back, is an accepted
risk under Q53.

**Admission (data policy §4a; Q153 (b), Q156).** Only work captures are admitted until the
wave-1 gate is met (P1.4 applied and the lint check passing); a personal capture is refused
with a spoken reason. Work captures that touch a client or an engagement are refused (§6:
the system cannot check that the contract allows it). Nothing refused is kept: no draft,
no file, no log line with the text.

Refusal replies are fixed strings chosen by class only; they never quote the capture.
"""
import re
import unicodedata

MAX_CAPTURE = 600            # characters, after whitespace is collapsed

# §4a, wave 1: personal material waits for P1.4 and the lint check. A code constant, not a
# setting: opening it is a reviewed change, never a flag someone flips.
WAVE1_GATE_MET = False

DOMAINS = {"work": "professional", "personal": "personal"}

# The four D69 axes for an admitted capture, plus the D59 fields. Wave-1 labels for
# ordinary material (§4a): ordinary, approved external, private.
LABELS = (
    ("classification", "internal"),
    ("domain", None),                  # from David's answer
    ("sensitivity", "ordinary"),
    ("processing", "approved-external"),
    ("sharing", "private"),
    ("source", "voice-capture"),
    ("note_type", "capture"),
    ("review_status", "unreviewed"),
)

REFUSALS = {
    "empty": "Nothing was said, so nothing was kept.",
    "too_long": "That is too long for a quick note (%d characters at most). Nothing was kept."
                % MAX_CAPTURE,
    "unreadable": "The note had characters I can't save. Nothing was kept.",
    "domain": "I didn't get whether this is work or personal, so nothing was kept.",
    "personal": "Personal notes can't be saved from the phone yet: they open once the "
                "labelling update (P1.4) is done. Nothing was kept.",
    "secret": "That sounds like a password, key or code. Those never go in your notes. "
              "Nothing was kept.",
    "id_number": "That has a long number, like an account, card or ID number. Those never "
                 "go in your notes. Nothing was kept.",
    "engagement": "That sounds like client or engagement material, which can't go in your "
                  "notes. Nothing was kept.",
    "child": "That mentions a child or a school. Children's details are only added by hand, "
             "at a desk. Nothing was kept.",
    "health": "That sounds like health information, which can't be saved from the phone. "
              "Nothing was kept.",
    "finance": "That sounds like personal finances, which can't be saved from the phone. "
               "Nothing was kept.",
    "journal": "That sounds like a journal entry, which can't be saved from the phone. "
               "Nothing was kept.",
}

# Word lists, English and French (D121), matched on folded text (lower case, accents
# removed, punctuation as spaces). A term ending in "*" matches any word starting with it;
# a term with spaces matches those words in sequence. Order of classes = order of checks.
TERMS = {
    "secret": (
        # Words that introduce a secret value. Not "token" or "API key" alone: they are
        # ordinary words in David's AI work; the value itself is caught by its shape.
        "password*", "passwd", "passcode*", "passphrase*", "mot de passe", "mots de passe",
        "mdp", "pin code", "code pin", "my pin", "mon code", "recovery code*",
        "code de recuperation", "private key*", "cle privee", "secret key*", "cle secrete",
        "key is", "token is", "code is", "le code est", "la cle est", "seed phrase",
    ),
    "id_number": (
        "iban", "account number*", "numero de compte", "card number*",
        "numero de carte", "cvv", "cvc", "passport*", "passeport*", "id card*",
        "carte d identite", "identity card*", "national number", "numero national",
        "registre national", "social security", "securite sociale", "ssn", "niss",
        "tax number", "numero de tva", "vat number", "licence number", "permis de conduire",
        "driving licence", "driver s license",
    ),
    "engagement": (
        "client*", "customer*", "engagement*", "counterpart*", "contrepartie*", "mission*",
        "contract*", "contrat*", "sow", "statement of work", "nda", "deliverable*",
        "livrable*", "cahier des charges", "rfp", "rfi", "appel d offre*", "tender*",
        "prospect*", "invoice*", "facture*", "purchase order*", "bon de commande",
        "proposal for", "acme*",
    ),
    "child": (
        "child*", "kid", "kids", "kiddo*", "my son*", "our son*", "his son*", "her son*",
        "daughter*", "baby", "babies",
        "toddler*", "teen*", "boy", "boys", "girl", "girls", "enfant*", "fils", "fille*",
        "bebe*", "gamin*", "gamine*", "ado", "ados", "adolescent*", "garcon*", "school*",
        "ecole*", "creche*", "kindergarten*", "maternelle*", "primaire", "college*",
        "lycee*", "scout*", "nanny", "babysit*", "baby sit*", "nounou*", "garderie*",
        "pediatr*", "pupil*", "homework",
    ),
    "health": (
        "health", "sante", "doctor*", "docteur*", "medecin*", "physician*", "specialist*",
        "specialiste*", "hospital*", "hopita*", "clinic*", "clinique*", "dentist*",
        "therap*", "psych*", "medic*", "pharma*", "diagnos*", "symptom*", "illness*",
        "malad*", "sick*", "disease*", "cancer*", "surgery", "surgeon*", "chirurg*",
        "injur*", "blessure*", "back pain", "in pain", "douleur*", "pregnan*", "enceinte", "prescription*",
        "ordonnance*", "vaccin*", "allerg*", "blood", "sang", "sanguin*", "mental*",
        "depress*", "anxi*", "burnout*", "burn out", "insomn*", "appointment with dr",
        "rendez vous chez", "rdv chez", "kine*", "physio*", "osteo*", "gyneco*",
        "cardio*", "dermato*", "radiolog*", "irm", "mri", "x ray",
    ),
    "finance": (
        "salary*", "salaire*", "payslip*", "fiche de paie", "loan*", "emprunt*",
        "mortgage*", "hypothe*", "credit card*", "carte de credit", "carte bancaire",
        "bank account*", "bank card*", "bank details", "my bank", "ma banque",
        "compte bancaire", "coordonnees bancaires", "debt*", "dette*", "tax return*",
        "declaration fiscale", "impot*", "taxes", "net worth", "patrimoine*", "savings",
        "epargne*", "pension*", "retraite*", "inheritance*", "my investments",
        "mes investissements", "stock options", "brokerage", "courtier*", "my crypto",
        "bitcoin*", "overdraft", "decouvert",
    ),
    "journal": (
        "diary", "dear diary", "journal intime", "my journal", "mon journal",
        "cher journal",
    ),
}

# Proper names in David's work that contain a listed word, removed before matching. Only
# exact phrases, and only these: a new one is a reviewed change.
ALLOWED_PHRASES = ("product school",)

_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "un", "une", "deux", "trois", "quatre", "cinq", "sept", "huit", "neuf", "oh",
}

_TOKEN_PREFIX = re.compile(r"(?i)(?:\bsk-|\bghp_|\bgho_|\bghs_|\bxox[abpr]-|\bAKIA|\bAIza|"
                           r"\beyJ|BEGIN [A-Z ]*KEY|-----)")
# Key-shaped runs: no hyphen or dot inside, so model names ("gemma-4-26B-A4B") pass.
_KEYLIKE = re.compile(r"[A-Za-z0-9_+/=]{16,}")
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-+/=.]{40,}")
_DIGIT_GROUPS = re.compile(r"\d+(?:[ .\-/]\d+)*")
_CODE = re.compile(r"(?i)\b(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]{6,}\b")
_IBAN = re.compile(r"(?i)\b[a-z]{2}\d{2}(?:[ ]?[a-z0-9]{4}){3,}")


def fold(text):
    """Lower case, accents removed, anything not a letter or digit as a space."""
    t = unicodedata.normalize("NFKD", text.casefold())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("ß", "ss").replace("æ", "ae").replace("œ", "oe").replace("ø", "o")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _matches(words, term):
    parts = term.split()
    n = len(parts)
    for i in range(len(words) - n + 1):
        ok = True
        for j, p in enumerate(parts):
            w = words[i + j]
            if p.endswith("*"):
                if not w.startswith(p[:-1]):
                    ok = False
                    break
            elif w != p:
                ok = False
                break
        if ok:
            return True
    return False


def _term_class(words):
    for cls, terms in TERMS.items():
        if any(_matches(words, t) for t in terms):
            return cls
    return None


def _looks_secret(text):
    if _TOKEN_PREFIX.search(text) or _LONG_RUN.search(text):
        return True
    for w in _KEYLIKE.findall(text):
        letters = sum(c.isalpha() for c in w)
        digits = sum(c.isdigit() for c in w)
        if letters >= 2 and digits >= 2:
            return True
    return False


def _looks_id_number(text, words):
    for m in _DIGIT_GROUPS.finditer(text):
        if sum(c.isdigit() for c in m.group(0)) >= 9:
            return True
    m = _IBAN.search(text)
    if m and sum(c.isdigit() for c in m.group(0)) >= 8:
        return True
    for m in _CODE.finditer(text):
        if sum(c.isdigit() for c in m.group(0)) >= 6:   # passport- or reference-like code
            return True
    run = 0
    for w in words:                                    # digits spelled out by dictation
        run = run + 1 if (w in _NUMBER_WORDS or w.isdigit()) else 0
        if run >= 9:
            return True
    return False


def clean(text):
    """The text as it will be saved and read back: Unicode NFC, every run of whitespace
    one space, trimmed. Returns None if it holds characters that cannot be saved."""
    if not isinstance(text, str):
        return None
    t = unicodedata.normalize("NFC", text)
    for c in t:
        cat = unicodedata.category(c)
        if c in "\t\n\r":
            continue
        # Control, format (bidi overrides, zero-width), surrogate, private-use, unassigned.
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return None
    return " ".join(t.split())


def domain_of(answer):
    """David's answer to "work or personal?": "work", "personal", or None (unclear)."""
    if not isinstance(answer, str):
        return None
    a = answer.strip().casefold()
    return a if a in DOMAINS else None


def check(text, domain_answer):
    """(None, (text, domain)) for an admissible capture, or (reason_key, None).

    Checks run in a fixed order: form, domain, admission, secret words, number shapes,
    key shapes, then the other word classes. The first hit decides the reason; a capture
    is never saved in part."""
    t = clean(text)
    if t is None:
        return "unreadable", None
    if not t:
        return "empty", None
    if len(t) > MAX_CAPTURE:
        return "too_long", None
    domain = domain_of(domain_answer)
    if domain is None:
        return "domain", None
    if domain == "personal" and not WAVE1_GATE_MET:
        return "personal", None
    words = fold(t).split()
    folded = " " + " ".join(words) + " "
    for phrase in ALLOWED_PHRASES:
        folded = folded.replace(" %s " % phrase, " ")
    cls = _term_class(folded.split())
    if cls == "secret":
        return cls, None
    if _looks_id_number(t, words):
        return "id_number", None
    if _looks_secret(t):
        return "secret", None
    if cls:
        return cls, None
    return None, (t, domain)


_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NAME = re.compile(r"\d{4}-\d{2}-\d{2}-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\.md")


def slug(text, words=6, limit=48):
    s = "-".join(fold(text).split()[:words])[:limit].strip("-")
    return s or "capture"


def render(text, domain, now):
    """(file name, exact bytes) of the Inbox note for an admitted capture. `now` is a
    naive local datetime (Europe/Brussels). The frontmatter is flat `key: value` lines
    with bare values only, so the index and the commit gate read it unambiguously."""
    t, d = clean(text), domain_of(domain)
    if not t or d is None:
        raise ValueError("render needs an admitted capture")
    name = "%s-%s.md" % (now.strftime("%Y-%m-%d-%H%M"), slug(t))
    lines = ["---"]
    for key, value in LABELS:
        lines.append("%s: %s" % (key, DOMAINS[d] if key == "domain" else value))
    lines += ["created: %s" % now.strftime("%Y-%m-%d"),
              "updated: %s" % now.strftime("%Y-%m-%d"),
              "---",
              "# Phone capture, %s %d %s %d, %s" % (_DAYS[now.weekday()], now.day,
                                                   _MONTHS[now.month - 1], now.year,
                                                   now.strftime("%H:%M")),
              "",
              t,
              ""]
    return name, "\n".join(lines).encode("utf-8")


def read_back(text, domain):
    """What the phone says before David decides: everything that will be saved that he
    did not choose himself is fixed, so this names the label and repeats the words."""
    label = "Work" if domain_of(domain) == "work" else "Personal"
    return "%s note for your Inbox: %s. Say Save to keep it." % (label, clean(text))


# PL1 step 7, append-only correction (D143). A correction is one more Inbox note, written by
# the same writer under the same gate, that names the note it corrects in `corrects:`; the
# index then leaves the named note out of answers (`index.build`). No existing file is ever
# edited. The whole named note leaves answers, not just the wrong line: accepted in D143.
CORRECTION_TYPE = "correction"
# The target goes into the frontmatter as a bare value, so it must be a path that both the
# index grammar and YAML read back exactly: word characters, spaces and a few marks, no
# colon, `#`, quote, bracket, pipe or backslash; each segment starts with a letter or digit.
TARGET = re.compile(r"[^\W_][\w .,()-]*(?:/[^\W_][\w .,()-]*)*\.md")
MAX_TARGET = 200
_DATE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-")

CORRECT_REFUSALS = {
    "unknown": "Too late to correct that answer: it is over an hour old, or the service "
               "restarted. Ask again, then correct it. Nothing was kept.",
    "source": "That answer has no source with that number. Nothing was kept.",
    "target": "That note can't be corrected from the phone; change it in Obsidian. "
              "Nothing was kept.",
    "changed": "That note changed after the answer. Ask again, then correct it. Nothing was "
               "kept.",
}


def check_target(path):
    """None if `path` may be named by a correction, else a refusal key. The path is saved
    too, so it goes through the same word lists and, segment by segment, the same number
    and key shapes as the text; only a leading Inbox date stamp (`YYYY-MM-DD-HHMM-`) is
    exempt, since it would always look like an ID number (review 1/2, session 59)."""
    if not isinstance(path, str) or len(path) > MAX_TARGET or not TARGET.fullmatch(path):
        return "target"
    if path != unicodedata.normalize("NFC", path) or clean(path) != path:
        return "target"
    for segment in path[:-3].split("/"):
        rest = _DATE_STAMP.sub("", segment, count=1)
        if _looks_id_number(rest, fold(rest).split()):
            return "id_number"
        if _looks_secret(rest):
            return "secret"
    words = " " + fold(path[:-3]) + " "
    for phrase in ALLOWED_PHRASES:
        words = words.replace(" %s " % phrase, " ")
    return _term_class(words.split())


def render_correction(text, domain, target, now):
    """(file name, exact bytes) of the Inbox note for an admitted correction of `target`.
    Same labels as a capture, `note_type: correction`, and `corrects: <target>`."""
    t, d = clean(text), domain_of(domain)
    if not t or d is None or check_target(target) is not None:
        raise ValueError("render_correction needs an admitted correction and target")
    name = "%s-correction-%s.md" % (now.strftime("%Y-%m-%d-%H%M"), slug(t, words=5))
    lines = ["---"]
    for key, value in LABELS:
        if key == "domain":
            value = DOMAINS[d]
        elif key == "note_type":
            value = CORRECTION_TYPE
        lines.append("%s: %s" % (key, value))
    lines += ["corrects: %s" % target,
              "created: %s" % now.strftime("%Y-%m-%d"),
              "updated: %s" % now.strftime("%Y-%m-%d"),
              "---",
              "# Correction from the phone, %s %d %s %d, %s" % (_DAYS[now.weekday()], now.day,
                                                               _MONTHS[now.month - 1], now.year,
                                                               now.strftime("%H:%M")),
              "",
              t,
              "",
              "Replaces in answers the note %s." % target,
              ""]
    return name, "\n".join(lines).encode("utf-8")


def target_title(target):
    """The corrected note as the phone names it: its file name without folder or `.md`."""
    return target.rsplit("/", 1)[-1][:-3]


def read_back_correction(text, domain, target):
    label = "Work" if domain_of(domain) == "work" else "Personal"
    return ("%s correction for your Inbox: %s. The note %s will no longer be used in "
            "answers. Say Save to keep it." % (label, clean(text).rstrip("."),
                                                target_title(target)))
