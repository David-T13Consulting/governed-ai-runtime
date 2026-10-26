"""MVP-phone: `POST /ask` over Tailscale, so David can ask from his iPhone (brief MVP-phone).

    python3 -m dbrain.serve [--host 100.x.y.z] [--port 8765]

It is a thin, read-only shell around `answer.ask()` with the whole-vault profile. What
it adds is the boundary:

  * **Bind:** only a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48) or loopback.
    `0.0.0.0`, `::` and LAN addresses are refused at start-up; without `--host` the
    address comes from the Tailscale CLI, and the service does not start if there is none.
  * **Peers:** every request's source address is checked against the same ranges.
  * **Auth:** `Authorization: Bearer <token>`, compared in constant time. The token is
    read from the macOS Keychain at start-up and kept only in memory: never in a file,
    an argument, an environment variable or a log (data policy §2.2).
  * **Load:** one question at a time (a second gets 429), 4 KB body cap, 500-character
    question cap, and one end-to-end deadline: 120 s after the request arrived the phone
    gets 504, whatever stage is still running (step 5). A late model call keeps the
    one-at-a-time lock until it returns, so it never overlaps the next question.
  * **Nothing executes:** the question is text for retrieval and the prompt; vault
    content is evidence, never instructions; /ask writes nothing to the vault. Traces go
    to the git-ignored `artifacts/` folder through the write guard.
  * **One write (PL1 step 6):** `POST /capture` drafts one Inbox note and reads it back;
    `POST /capture/save` writes that exact draft once, through `writer.py`, the only code
    that writes inside the vault (one new file in `00 Inbox/`, never an edit).
  * **Corrections (PL1 step 7, D143) are append-only:** `POST /correct` drafts one more
    Inbox note naming a note the answer cited; saved through `/capture/save`, it makes the
    index leave the named note out of answers. No existing file is edited.

`GET /health` answers without auth and without any vault content, so the Shortcut can
tell "the Air is offline or asleep" from "the brain has no answer".
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import socket
import sys
import threading
import time
import unicodedata
import urllib.request
from collections import OrderedDict
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapter as adapter_mod
from . import aliases, answer, capture, config, index, outpath, scope, trace, writer
from . import failures as failures_mod

# Payload key carrying the verdict's reason code from Service.ask to the capture point;
# never sent to the phone.
REASON_KEY = "_reason_code"

TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
KEYCHAIN_SERVICE = "dbrain-mvp-ask"
MAX_BODY = 4096
MAX_QUESTION = 500
DEFAULT_PORT = 8765
REQUEST_TIMEOUT = 120
TAILSCALE_CLIS = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")

# P2.7 step C5, the phone's one-tap "wrong" (data policy v0.8 §7a, D123): each recent
# answer's text is held in this process's memory only, never on disk, for at most
# RECENT_HOLD seconds. A tap is accepted for RECENT_WINDOW; the sweeper runs every
# RECENT_SWEEP and drops anything that old, so nothing is held past RECENT_HOLD while the
# Mac is awake (a sleeping Mac runs nothing; the first sweep after waking drops it).
RECENT_HOLD = 3600
RECENT_SWEEP = 15
RECENT_WINDOW = RECENT_HOLD - RECENT_SWEEP
RECENT_MAX = 64                       # answers held at once; the oldest is dropped first
ANSWER_ID = re.compile(r"[0-9a-f]{32}")
WRONG_SIGNAL = "user_wrong"
# /wrong replies: fixed text, chosen by outcome only, never by what the phone sent.
WRONG_REPLIES = {
    "recorded": "Marked wrong. It is kept on this Mac for the weekly review.",
    "repeat": "Already marked wrong.",
    "unknown": "Too late to mark this answer: it is over an hour old, or the service restarted.",
    "off": "Marking answers wrong is switched off on this Mac.",
    "error": "Could not mark it wrong; try again.",
}

# PL1 step 6, "remember from the phone" (P3.0 Part 1). A draft is the exact bytes of one
# Inbox note, frozen before the read-back and held in this process's memory only, for at
# most DRAFT_TTL seconds; it is written once, on "save", and never again. Refused captures
# leave nothing: no draft, no file, no log line with the text. Nothing here reaches the
# model, the traces or the failure store.
DRAFT_TTL = 300
DRAFT_MAX = 16
DRAFT_ID = ANSWER_ID
MAX_DOMAIN = 16
# /capture/save replies: fixed text, chosen by outcome only.
SAVE_REPLIES = {
    "unknown": "Nothing to save: that note is over 5 minutes old, or the service restarted. "
               "Say it again.",
    "repeat": "Already saved.",
    "busy": "Still saving that note.",
    "error": "Not saved: something went wrong on the Air. Say it again.",
    "check": "Saved, but I couldn't confirm it. Check the Inbox on the Air before saying it "
             "again.",
    "left": "Not saved. A hidden temporary file was left in the Inbox on the Air; it needs "
            "deleting there.",
    "none": "Nothing was kept, so there is nothing to save.",
    "changed": "Not saved: the note it corrects changed in the meantime. Ask again, then "
               "correct it.",
}
CAPTURE_BAD_REQUEST = "The note was empty, too long or not sent as text. Nothing was kept."
# PL1 step 7 (D143): the notes each recent answer cited, as (path, SHA-256) pairs, so the
# phone names a correction's target by answer id and source number, never by a path. Paths
# and hashes only, in memory, for as long as a "wrong" tap is accepted.
CORRECT_SOURCE = re.compile(r"[1-9]")
# The model rewords the dictated correction once (D143); a slow or busy model is skipped and
# David's own words are used. Either way he hears the exact text before "save".
CORRECT_MODEL_TIMEOUT = 20
CORRECT_MAX_TOKENS = 120
CORRECT_PROMPT = (
    "David says an answer from his notes was wrong and dictated this correction:\n\n"
    "{text}\n\n"
    "Rewrite it as one short standalone statement of the correct fact, in the same language. "
    "Use only what he said; add nothing. Reply with the statement only.")
ROUTES = ("/ask", "/health", "/wrong", "/capture", "/capture/save", "/correct")


class BindRefused(Exception):
    """An address this service must not listen on."""


def allowed_address(addr):
    """Tailnet or loopback. Everything else, including the LAN and 0.0.0.0, is refused."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in TAILNET_NETS)


def check_bind(host):
    if host in ("", "0.0.0.0", "::", "*") or not allowed_address(host):
        raise BindRefused(
            "refusing to listen on %r: only a Tailscale address or loopback is allowed" % host
        )
    return host


def tailscale_ipv4():
    """The Air's Tailscale address, from the Tailscale CLI, or None."""
    for cli in TAILSCALE_CLIS:
        path = shutil.which(cli) or (cli if os.path.isabs(cli) and os.path.exists(cli) else None)
        if not path:
            continue
        try:
            out = subprocess.run([path, "ip", "-4"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.stdout.split():
            if allowed_address(line) and not ipaddress.ip_address(line).is_loopback:
                return line
    return None


def keychain_token(service=KEYCHAIN_SERVICE):
    """Read the bearer token from the login Keychain. Never logged, never written."""
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit("cannot read the token from the Keychain: %s" % exc)
    token = out.stdout.strip()
    if out.returncode != 0 or len(token) < 32:
        raise SystemExit(
            "no usable token in the Keychain item %r (run mvp/make-token.sh first)" % service
        )
    return token


# Fact-first wording for the phone (David, MVP acceptance, 5 Oct): "Yes, there's no
# school on 2 Nov" put a yes/no lead-in in front of the fact and contradicted it. P2.2's
# PROMPT is left untouched, so its pinned prompt version still describes run B.
MVP_PROMPT = answer.PROMPT.replace(
    "- Do not guess, and do not use knowledge from outside the evidence.\n",
    "- Do not guess, and do not use knowledge from outside the evidence.\n"
    "- Start with the fact itself, as a full sentence (for example \"There is no school "
    "on Mon 2 Nov.\"). Do not begin with Yes or No.\n"
    "- Keep it short: one to three sentences.\n",
)
assert MVP_PROMPT != answer.PROMPT

# Date awareness (David, Q46 priority 2, 5 Oct): "What's on the menu tomorrow?" was
# answered about 2 Nov, because nothing knew what day it was. Relative dates are resolved
# against the Air's date in Brussels *before retrieval*, written into the question in the
# forms the notes use ("Tue 06" under a month heading, "Tue 6 Oct"), and the model is told
# today's date and to answer for that date or say it found nothing for it.
TIMEZONE = "Europe/Brussels"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")
_WEEKDAY_WORDS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
    "sunday": 6, "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4,
    "samedi": 5, "dimanche": 6,
}
_WD = "|".join(_WEEKDAY_WORDS)
# Dates are read in three steps (Codex consultation, session 20, after review rounds 5-7
# each found a new form): (1) recognise components separately (explicit dates, standalone
# phrases, weekdays, week references), keeping their spans; (2) combine a weekday with an
# adjacent week reference, in either order, through a small connector grammar; (3) resolve.
# A date that cannot be read confidently is "unclear": the service asks again rather than
# guess. Explicit dates win over anything they overlap.
_STANDALONE = re.compile(
    r"\b(?:day after tomorrow|apr[eè]s-demain|day before yesterday|avant-hier|tomorrow|demain|"
    r"today|aujourd'hui|yesterday|hier|"
    r"(?:this|ce|next|coming|le) (?:weekend|week-end)(?: prochain)?)\b", re.I)
_WEEK = re.compile(r"\b(?:(?:this|next) week(?:'s)?|cette semaine|(?:la )?semaine prochaine)(?![\w-])", re.I)
_WEEKDAY = re.compile(r"\b(?:(?P<pre>on|this|coming|next|ce|le) )?(?P<wd>%s)(?: (?P<post>prochain))?\b"
                      % "|".join(_WEEKDAY_WORDS), re.I)
# What may sit between a weekday and its week: spaces, a comma, and one of: of, in, during,
# de, de la. Nothing else, so "Thursday and next week" stays two targets.
_CONNECT = re.compile(r"^[ ,]*(?:(?:of|in|during|de la|de) )?[ ,]*$", re.I)

# Explicit dates: "Mon 2 Nov", "2 November 2026", "Monday, October 12", "le 2 novembre".
# Left as written in the question, but listed in `found` like relative ones.
_MONTH_WORDS = {}
for _n, _names in enumerate((
        ("jan", "january", "janvier", "janv"), ("feb", "february", "fevrier", "février", "févr", "fevr"),
        ("mar", "march", "mars"), ("apr", "april", "avril", "avr"), ("may", "mai"),
        ("jun", "june", "juin"), ("jul", "july", "juillet", "juil"),
        ("aug", "august", "août", "aout"), ("sep", "sept", "september", "septembre"),
        ("oct", "october", "octobre"), ("nov", "november", "novembre"),
        ("dec", "december", "décembre", "decembre", "déc")), 1):
    for _w in _names:
        _MONTH_WORDS[_w] = _n
_MON = "|".join(sorted(_MONTH_WORDS, key=len, reverse=True))
_WDA = "|".join(sorted(list(_WEEKDAY_WORDS) + ["mon", "tue", "tues", "wed", "thu", "thur",
                                                "thurs", "fri", "sat", "sun"],
                       key=len, reverse=True))
_YEAR_TAIL = r"(?:,?\s*\(?(?P<y%d>\d{4})\)?(?!\d))?"
_EXPLICIT = re.compile(
    r"\b(?:(?P<w1>%s)\.?,?\s+)?(?:the\s+|le\s+)?(?P<d1>\d{1,2})(?:st|nd|rd|th|er)?\s+(?:of\s+)?"
    r"(?P<m1>%s)\b\.?" % (_WDA, _MON) + _YEAR_TAIL % 1 +
    r"|\b(?:(?P<w2>%s)\.?,?\s+)?(?P<m2>%s)\b\.?\s+(?:the\s+)?(?P<d2>\d{1,2})(?:st|nd|rd|th)?(?!\d)"
    % (_WDA, _MON) + _YEAR_TAIL % 2, re.I)


def _explicit_date(m, today):
    """The date an explicit match names, or None if it is not a real date or its weekday
    disagrees ("lundi 6 octobre" in 2026). Without a year: this year, or next year if that
    would be more than about six months ago."""
    day = int(m.group("d1") or m.group("d2"))
    month = _MONTH_WORDS[(m.group("m1") or m.group("m2")).lower()]
    year = m.group("y1") or m.group("y2")
    try:
        if year:
            d = date(int(year), month, day)
        else:
            d = date(today.year, month, day)
            if d < today - timedelta(183):
                d = date(today.year + 1, month, day)
    except ValueError:
        return None
    wd = (m.group("w1") or m.group("w2") or "").lower()
    if wd:
        named = _WEEKDAY_WORDS[wd] if wd in _WEEKDAY_WORDS else [x.lower() for x in _DAYS].index(wd[:3])
        if named != d.weekday():
            return None
    return d


def today_local():
    """Today in Brussels. No silent fallback to the host's zone: without a time-zone
    database this raises, and main() refuses to start."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TIMEZONE)).date()


def now_local():
    """The time in Brussels, naive (for an Inbox note's name and date). Same rule as
    today_local: no fallback zone."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def fmt_day(d):
    """`Tue 6 Oct 2026`, the form the answer states."""
    return "%s %d %s %d" % (_DAYS[d.weekday()], d.day, _MONTHS[d.month - 1], d.year)


def days_label(days):
    """One day, a run of consecutive days ("Mon 5 Oct 2026 to Sun 11 Oct 2026"), or a list
    of separate days: separate dates are never shown as one range."""
    days = sorted(set(days))
    if len(days) == 1:
        return fmt_day(days[0])
    if all((b - a).days == 1 for a, b in zip(days, days[1:])):
        return "%s to %s" % (fmt_day(days[0]), fmt_day(days[-1]))
    return ", ".join(fmt_day(d) for d in days)


def _search_forms(d):
    """The ways a note may write this date, for lexical retrieval."""
    return "%s %d %s %d; %s %02d %s" % (_DAYS[d.weekday()], d.day, _MONTHS[d.month - 1], d.year,
                                        _DAYS[d.weekday()], d.day, _MONTH_NAMES[d.month - 1])


def _standalone_days(p, today):
    p = p.lower().replace("è", "e")
    if p in ("today", "aujourd'hui"):
        return [today]
    if p in ("tomorrow", "demain"):
        return [today + timedelta(1)]
    if p in ("day after tomorrow", "apres-demain"):
        return [today + timedelta(2)]
    if p in ("yesterday", "hier"):
        return [today - timedelta(1)]
    if p in ("day before yesterday", "avant-hier"):
        return [today - timedelta(2)]
    # a weekend: the coming Sat-Sun (this one if we are in it); next / prochain: a week on
    sat = today - timedelta(today.weekday()) + timedelta(5)
    if p.startswith("next") or "prochain" in p:
        sat += timedelta(7)
    return [sat, sat + timedelta(1)]


def _week_offset(text):
    """0 for this week, 7 for next week."""
    t = text.lower()
    return 7 if ("next" in t or "prochaine" in t) else 0


def _weekday_days(m, today):
    wd = _WEEKDAY_WORDS[m.group("wd").lower()]
    ahead = (wd - today.weekday()) % 7
    if (m.group("pre") or "").lower() == "next" or m.group("post"):
        ahead = ahead or 7  # "next Monday" on a Monday is a week away
    return [today + timedelta(ahead)]


def resolve_dates(question, today):
    """(question for retrieval and the model, [(phrase, [dates])]). Each relative phrase is
    kept and followed by its absolute date(s), so both retrieval and the model see them.
    Explicit dates are left as written but listed too. An entry with no dates is unclear
    (an impossible date, or a weekday whose week contradicts it): the caller asks again."""
    # Curly apostrophes read like straight ones. Same length, so spans stay aligned.
    question = question.replace("’", "'").replace("‘", "'")
    taken = []  # (start, end, phrase, days, rewrite)

    def free(s, e):
        return not any(a < e and s < b for a, b, _, _, _ in taken)

    for m in _EXPLICIT.finditer(question):  # reserved even when invalid: "Monday 31 Feb"
        d = _explicit_date(m, today)
        taken.append((m.start(), m.end(), m.group(0), [d] if d else [], False))
    for m in _STANDALONE.finditer(question):
        if free(m.start(), m.end()):
            taken.append((m.start(), m.end(), m.group(0), _standalone_days(m.group(0), today), True))
    weeks = [m for m in _WEEK.finditer(question) if free(m.start(), m.end())]
    wdays = [m for m in _WEEKDAY.finditer(question) if free(m.start(), m.end())]
    used_w = set()
    monday = today - timedelta(today.weekday())
    for wm in wdays:
        partner = None
        for k in weeks:
            if id(k) in used_w:
                continue
            gap = question[wm.end():k.start()] if wm.end() <= k.start() else question[k.end():wm.start()]
            if (wm.end() <= k.start() or k.end() <= wm.start()) and _CONNECT.match(gap):
                partner = k
                break
        if partner is None:
            stacked = (wm.group("pre") or "").lower() in ("this", "next", "coming") and wm.group("post")
            taken.append((wm.start(), wm.end(), wm.group(0),
                          [] if stacked else _weekday_days(wm, today), True))  # "next jeudi prochain"
            continue
        used_w.add(id(partner))
        s, e = min(wm.start(), partner.start()), max(wm.end(), partner.end())
        pre = (wm.group("pre") or "").lower()
        offset = _week_offset(partner.group(0))
        if pre == "next" or (wm.group("post") and offset == 0):
            days = []  # "next Thursday this week" contradicts; "next Thursday next week" stacks
        else:
            days = [monday + timedelta(offset + _WEEKDAY_WORDS[wm.group("wd").lower()])]
        taken.append((s, e, question[s:e], days, True))
    for k in weeks:
        if id(k) not in used_w:
            off = _week_offset(k.group(0))
            taken.append((k.start(), k.end(), k.group(0),
                          [monday + timedelta(off + i) for i in range(7)], True))
    taken.sort(key=lambda t: (t[0], t[1]))
    # Conservative by design (session 20, after review rounds 5-8 each found new phrasings):
    # the service resolves one plain date phrase per question and asks again otherwise.
    # Fail-closed (session 31, after round 9): any date-like text left over once the
    # supported phrases are consumed also makes the question unclear, so a form the
    # parser does not know is never passed on as if there were no date.
    hits = _leftover(question, taken)
    if hits or (taken and (len(taken) > 1 or _qualified(question, taken)
                           or _stray_year(question, taken))):
        spans = [(s, e) for s, e, _, _, _ in taken] + hits
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        return question, [(question[start:end].strip(" ,"), [])]
    out, found, pos = [], [], 0
    for start, end, phrase, days, rewrite in taken:
        found.append((phrase, days))
        out.append(question[pos:end])
        pos = end
        if not rewrite or not days:
            continue
        if len(days) > 2:  # a week: the span, then each day as the notes write it ("Tue 6 Oct")
            each = ", ".join(fmt_day(d).rsplit(" ", 1)[0] for d in days)
            out.append(" (%s: %s)" % (days_label(days), each))
        else:
            out.append(" (%s)" % "; ".join(_search_forms(d) for d in days))
    out.append(question[pos:])
    return "".join(out), found


# Words that change what a date phrase means when they sit right next to it ("the day
# before Monday", "last Friday", "Monday after next", "avant-hier"): the phrase alone would
# name the wrong day, so the question is unclear.
_MODIFIERS = {"before", "after", "last", "previous", "past", "following", "since", "until",
              "till", "from", "avant", "apres", "après", "dernier", "derniere", "dernière",
              "precedent", "précédent", "suivant", "suivante", "depuis", "jusqu'a", "jusqu'à",
              "passe", "passé", "ago", "of", "by", "between", "except", "but", "coming"}


def _qualified(question, taken):
    for start, end, _, _, _ in taken:
        before = question[:start]
        if before[-1:] in ("-", "–", "—", "'") or (before[-1:].isalnum()):
            return True
        after = question[end:]
        if after[:1] in ("-", "–", "—") or after[:1].isalnum():
            return True
        prev = re.search(r"([\w'-]+)[ ,]*$", before)
        nxt = re.match(r"[ ,]*([\w'-]+)", after)
        words = [w.group(1).lower() for w in (prev, nxt) if w]
        if any(w in _MODIFIERS for w in words):
            return True
    return False


# Date-like text outside the supported phrases (session 31, fail-closed, after review
# rounds 9-10 and a Codex consultation). Not a list of what is understood: a description of
# what *looks like* a date, checked over the whole question once the supported phrases are
# blanked out. Anything found makes the question unclear, a bare month too ("menus for
# May", review round 11). A plural weekday ("Thursdays") names a habit, not a date, and
# passes.
_FULL_WEEKDAYS = tuple(_WEEKDAY_WORDS)
_PLURAL_WEEKDAYS = {w + "s" for w in _WEEKDAY_WORDS}
# Every unconsumed month name or weekday abbreviation is date-like wherever it sits, even
# where it is also a word ("May I", "mon menu", "sept personnes"): positional exceptions were
# bypassed three review rounds running (10-12; Codex consultation, session 31).
_WD_ABBR = {"mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
_ALWAYS = {"days", "jours", "weeks", "semaines", "weekend", "weekends", "week-end", "week-ends",
           "months", "fortnight", "fortnights", "quinzaine", "tonight", "tonite", "overmorrow"}
_SINGULAR = {"day", "jour", "journée", "journee", "week", "semaine", "month", "mois", "year",
             "years", "année", "annee", "années", "annees"}
_DAYPART = {"morning", "afternoon", "evening", "night", "matin", "après-midi", "apres-midi",
            "soir", "soirée", "soiree", "nuit", "midi"}
_THIS = {"this", "ce", "cet", "cette"}
_ARTICLE = _THIS | {"the", "le", "la"}
_STRONG = {"next", "last", "previous", "past", "following", "prochain", "prochaine",
           "prochains", "prochaines", "dernier", "dernière", "derniere", "derniers",
           "précédent", "précédente", "precedent", "precedente", "suivant", "suivante",
           "every", "each", "chaque", "other", "ago", "before", "after", "since", "until",
           "till", "avant", "après", "apres", "depuis", "jusqu'à", "jusqu'a", "jusqu'au",
           "between", "entre", "through", "thru", "except", "sauf", "not", "pas", "early",
           "late", "mid", "mi", "début", "debut", "fin"}
_NUMBER_WORDS = {"a", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                 "ten", "eleven", "twelve", "fifteen", "twenty", "thirty", "few", "couple",
                 "several", "un", "une", "deux", "trois", "quatre", "cinq", "sept", "huit",
                 "neuf", "dix", "onze", "douze", "quinze", "vingt", "trente", "quelques",
                 "plusieurs"}
_ORDINAL = re.compile(r"^(?:fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
                      r"\w+teenth|twentieth|thirtieth|(?:twenty|thirty)-\w+|\w+i[eè]me)$")
_LOW_ORDINAL = {"first", "second", "third", "premier", "première", "premiere"}
# A day name or "tomorrow" mistyped ("tomorow", "wenesday") still names a date.
_FUZZY = ("tomorrow", "yesterday", "tonight", "weekend", "aujourd'hui", "demain", "semaine",
          "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
          "mercredi", "vendredi", "samedi", "dimanche")
_NOT_TYPOS = {"domain", "sundae", "sundaes"}
# Time-direction words: with no date read, they may point at one ("the next weekday").
_DIRECTION = {"next", "last", "previous", "past", "following", "upcoming", "coming", "prochain",
              "prochaine", "prochains", "prochaines", "dernier", "dernière", "derniere",
              "derniers", "dernières", "précédent", "précédente", "precedent", "precedente",
              "suivant", "suivante", "ago", "avant-veille", "veille", "lendemain", "surlendemain",
              "eve"}
# More words that may point at a date when none was read (red-team pass before review
# round 14): texting shorthand, French and Dutch day words, short week words, school periods,
# "now" and hour offsets.
_POINTERS = {"tmrw", "tmw", "tmr", "tmrrw", "2moro", "2morrow", "tdy", "2day", "tday", "auj",
             "ajd", "dem1", "dmain", "lun", "mar", "mer", "jeu", "ven", "sam", "dim", "weds",
             "wk", "wks", "sem", "w-e", "wkend", "break", "breaks", "term", "terms",
             "half-term", "trimestre", "semester", "semestre", "rentrée", "rentree",
             "vacances", "congé", "conge", "congés", "conges", "holiday", "holidays",
             "détente", "detente", "strike", "grève", "greve", "saint", "saints", "st", "souls",
             "fête", "fete", "fêtes", "fetes", "nicolas", "sinterklaas", "valentin", "valentine",
             "epiphany", "épiphanie", "epiphanie", "chandeleur", "thanksgiving", "diwali", "eid",
             "hanukkah", "hallowe'en", "now", "maintenant", "currently", "actuellement",
             "moment", "hour", "hours", "heure", "heures", "lunchtime", "morgen", "vandaag",
             "gisteren", "overmorgen", "eergisteren", "volgende", "vorige", "komende",
             "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag",
             "januari", "februari", "maart", "mei", "juni", "juli", "augustus", "oktober"}
# Seasons (run B review): a period that may point at a date when none was read.
_SEASONS = {"winter", "summer", "spring", "autumn", "fall", "season", "seasons", "hiver", "été",
            "ete", "printemps", "automne", "saison", "saisons", "winters", "summers"}
# "a été", "ont été": the verb, never the season.
_AVOIR = {"a", "ai", "as", "avons", "avez", "ont", "avait", "avaient", "aura", "auront",
          "aurait", "auraient", "eu", "ayant", "n'a", "n'ont", "n'avait", "y"}
_POLITE = {"please", "pls", "plz", "thanks", "thank", "thx", "merci", "svp", "stp"}
_HOLIDAYS = {"christmas", "xmas", "noël", "noel", "easter", "pâques", "paques", "halloween",
             "toussaint", "carnaval", "carnival", "réveillon", "reveillon", "nouvel", "new-year",
             "nye", "pentecost", "pentecôte", "ascension", "assomption", "armistice"}
# Numbers that cannot be a date: a time of day, or a count with its unit or noun.
_TIME_OR_UNIT = re.compile(r"^\d{1,2}(?::\d{2}|h\d{0,2}|am|pm)$|^\d+(?:k|km|m|min|kg|g|l|€|%|x)$")
_COUNT = re.compile(r"\d{1,3}(?:[.,]\d+)?")
_COUNT_NOUNS = {"people", "persons", "person", "personnes", "personne", "kids", "children",
                "enfants", "adults", "adultes", "km", "minutes", "min", "hours", "heures",
                "euros", "eur", "times", "fois", "places", "seats", "portions", "%"}
# Tokens: a code (letters then digits: "P2.4", "D53", "Oct6"), a number not glued to a
# letter (after ":", "/", "." or "-" it still counts: "menu:2026-10-06"), or a word.
_TOKEN = re.compile(r"[^\W\d_]+\d[\w.]*|(?<![^\W_])\d+(?:[.,:/h-]\d+)*[^\W_]*|"
                    r"[^\W\d_]+(?:['-][^\W\d_]+)*", re.U)


def _near(a, b, limit):
    """Edit distance of a and b is at most limit (small words, small limits)."""
    if abs(len(a) - len(b)) > limit:
        return False
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    if row[-1] <= limit:
        return True
    # one swap of neighbouring letters counts as one edit ("demian")
    return any(a[:k] + a[k + 1] + a[k] + a[k + 2:] == b for k in range(len(a) - 1)) and limit >= 1


def _leftover(question, taken):
    """[(start, end)] of date-like text outside every consumed phrase. Empty means the
    question holds no date the parser has not fully read."""
    masked = list(question)
    for s, e, _, _, _ in taken:
        masked[s:e] = " " * (e - s)
    masked = "".join(masked)
    toks = [(m.start(), m.end(), m.group(0).lower()) for m in _TOKEN.finditer(masked)]
    hits, strong, undated, months = [], [], [], []

    def touches_span(s, e):  # only spaces between this token and a consumed phrase
        return any((b <= s and not question[b:s].strip()) or (e <= a and not question[e:a].strip())
                   for a, b, _, _, _ in taken)

    def date_word(w, prev, prev2, nxt):
        if w in _PLURAL_WEEKDAYS or w in _NOT_TYPOS:
            return False
        if w.startswith(_FULL_WEEKDAYS) or w in _WD_ABBR or w in _ALWAYS or _ORDINAL.match(w):
            return True
        if w in _MONTH_WORDS:
            return True
        if w in _SINGULAR:  # "next week" is read; "a week", "the week after", "Monday week" are not
            return (prev in _STRONG or prev in _NUMBER_WORDS or prev in _ARTICLE
                    or prev == "coming")
        if w in _LOW_ORDINAL and prev in ("twenty", "thirty", "vingt", "trente"):
            return True  # "the twenty first"
        if w in _LOW_ORDINAL:  # "on the first", "the second of November"; not "the second brain"
            return prev in ("the", "le", "la") and (not nxt or nxt in ("of", "de", "d")
                                                    or nxt in _MONTH_WORDS)
        if w in ("an", "ans"):  # "dans un an", "deux ans"
            return prev in _NUMBER_WORDS
        if w in _NUMBER_WORDS and w != "a":
            return prev in ("le", "du", "au")  # "le trois"
        if w in _DAYPART:
            return prev in _THIS
        return len(w) >= 5 and any(_near(w, f, 1 if len(f) <= 7 else 2) for f in _FUZZY)

    for i, (s, e, w) in enumerate(toks):
        prev = toks[i - 1][2] if i else ""
        prev2 = toks[i - 2][2] if i > 1 else ""
        nxt = toks[i + 1][2] if i + 1 < len(toks) else ""
        bare = w[:-2] if w.endswith("'s") else w
        if w[0].isdigit():
            count = (_COUNT.fullmatch(w) and nxt in _COUNT_NOUNS
                     and not masked[e:toks[i + 1][0]].strip())
            if not (_TIME_OR_UNIT.match(w) or count):
                hits.append((s, e))
            continue
        if re.match(r"[^\W\d_]+\d", w):  # a code: a date only if it starts with a day or month
            alpha = re.match(r"[^\W\d_]+", w).group(0)
            if (alpha in _MONTH_WORDS or alpha in _WD_ABBR or alpha in _WEEKDAY_WORDS
                    or alpha in ("w", "wk", "kw", "sem", "week", "semaine")):  # "W42"
                hits.append((s, e))
            continue
        parts = [bare] + (bare.split("-") if "-" in bare else [])  # "mid-October", "mi-octobre"
        if any(date_word(p, prev, prev2, nxt) for p in parts):
            hits.append((s, e))
        elif "-" in bare and any(p in _STRONG for p in parts) and any(p in _MONTH_WORDS for p in parts):
            hits.append((s, e))
        elif bare in _SINGULAR and touches_span(s, e):
            hits.append((s, e))
        else:
            if (bare in _SEASONS and not (bare in ("été", "ete") and prev in _AVOIR)
                    or bare in _HOLIDAYS or bare in _SINGULAR or bare in _DAYPART or bare in _POINTERS
                    or any(p in _POINTERS for p in parts) or bare.startswith(("maandag", "dinsdag",
                    "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"))
                    or any(p in _DIRECTION for p in parts)
                    or (re.search(r"(?:day|jour|journ[ée]e)s?$", bare)
                        and bare not in _PLURAL_WEEKDAYS)):
                undated.append((s, e))  # date-like only when nothing was read: see below
            if bare in _LOW_ORDINAL and prev in ("the", "le", "la"):
                months.append((s, e))  # "the third": a conflict only next to a date read
                # With no date read, "the first" may be a day of the month whatever follows
                # ("on the first, please?" round 14; "on the first if possible?" run B), so it
                # asks again; "the first race?" over-asks, the accepted cost. Not "second brain".
                if nxt != "brain":
                    undated.append((s, e))
            if any(p in _STRONG for p in parts) or bare.endswith("n't"):
                strong.append((s, e))
    # When a phrase was read, the answer always carries "(Asked about ...)", so an answer
    # about another date is visible. When none was read there is no label, so any word that
    # may point at a date makes the question unclear (review round 13: "the next weekday",
    # "New Year's Day"). Over-asking ("Which week is the break?") is the accepted cost.
    if not taken:
        hits += undated
    else:
        hits += months  # "yesterday the third"
    if hits or taken:
        hits += strong
    return hits


def _stray_year(question, taken):
    """A four-digit number outside every recognised date: possibly a year the parser did
    not attach ("October 6,2027" before the fix), so the date is not certain."""
    for m in re.finditer(r"(?<!\d)\d{4}(?!\d)", question):
        if not any(s <= m.start() and m.end() <= e for s, e, _, _, _ in taken):
            return True
    return False


def dated_prompt(today, found):
    """MVP_PROMPT with today's date and, when the question named a date, the rule to
    answer for that date only."""
    extra = "- Today is %s (%s).\n" % (fmt_day(today), TIMEZONE)
    days = [d for _, ds in found for d in ds]
    if days:
        named = days_label(days)
        extra += ("- The question is about %s. Begin the answer with that date (for example "
                  "\"%s: ...\"). If the evidence says nothing for that date, say \"Nothing found "
                  "for %s.\" and do not answer about another date.\n" % (named, named, named))
    return MVP_PROMPT.replace("- Keep it short", extra.replace("%", "%%") + "- Keep it short", 1)


CITE_ID = re.compile(r"\[E(\d+)\]")
_WORD = re.compile(r"[\w:'’-]+", re.U)
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "is", "on", "in",
         "of", "to", "a", "an", "at", "by", "it", "be", "or", "as", "there", "evidence"}


def citation(cite):
    """`path:start-end` -> a dict the Shortcut can show, with the folder made visible."""
    m = re.match(r"^(?P<path>.+):(?P<start>\d+)-(?P<end>\d+)$", cite)
    path, lines = (m["path"], "%s-%s" % (m["start"], m["end"])) if m else (cite, "")
    folder = path.split("/", 1)[0] if "/" in path else "(vault root)"
    return {
        "path": path,
        "lines": lines,
        "folder": folder,
        # Inbox notes are raw captures nobody has reviewed: say so next to the answer.
        "unreviewed": folder == "00 Inbox",
    }


def _words(text):
    return {w.lower().strip("'’-") for w in _WORD.findall(text)} - _STOP


def quote_lines(chunk_text, answer_text, k=2, width=160):
    """The note's own line(s) that best match the answer, verbatim, so a contradiction
    between the answer and its source is visible on the phone (acceptance fix 2)."""
    target = _words(CITE_ID.sub("", answer_text))
    scored = []
    for i, line in enumerate(chunk_text.splitlines()):
        bare = line.strip()
        if not bare or bare.startswith("#") or set(bare) <= set("|-: "):
            continue
        score = len(_words(bare) & target)
        if score:
            scored.append((score, i, bare))
    best = sorted(sorted(scored, reverse=True)[:k], key=lambda t: t[1])
    out = []
    for _, _, bare in best:
        out.append(bare if len(bare) <= width else bare[: width - 1] + "…")
    return out


NEGATION_REASON = "the answer says nothing is on, but a note it cites lists that day"


def check_dated_negation(result, days):
    """P2.5 F2: a bare "nothing on <day>" answer against a cited table row for that day
    becomes the defect `negation_vs_dated_evidence`, so P2.7 captures it. The phone keeps
    the model's answer and adds the row below it (David, 13 Oct, session 47: a wrong fire
    must never hide a correct answer). Answers that cite nothing are not checked (Q102)."""
    if not days or result.status not in (answer.INCOMPLETE, answer.WITHHELD) \
            or result.reason_code == answer.NO_EVIDENCE or not result.answer:
        return None
    # Exactly the ids answer.ask offered ("E1", not "E01" or "E١"); any other id and the
    # check stays silent (F2 D74 consult, 13 Oct). The heading goes with the text, so a
    # year or a doubtful word in it counts.
    by_id = {"E%d" % i: (i, c) for i, (_, c) in enumerate(result.hits, 1)}
    ids = answer.CITE.findall(result.answer)
    if any(cid not in by_id for cid in ids):
        return None
    cited = []
    for cid in dict.fromkeys(ids):
        label, chunk = by_id[cid]
        cited.append((label, "%s\n%s" % (chunk.heading or "", chunk.text)))
    hit = answer.dated_contradiction(result.answer, cited, days)
    if hit is None:
        return None
    label, line = hit
    if line.startswith("|"):  # a table row: its cells, without the pipes
        line = " · ".join(cell.strip() for cell in line.strip("|").split("|") if cell.strip())
    result.status = answer.INCOMPLETE
    result.reason_code = answer.NEGATION_VS_DATED
    result.reason = NEGATION_REASON
    result.answer += ("\n\nBut the note it cites lists this for that day: “%s” [E%d]. "
                      "Check the note." % (line, label))
    return hit


def plain_status(status, reason, citations):
    """One line David can read on the phone, instead of the engineering reason."""
    reason = reason or ""
    if status == "supported":
        line = "Supported by the cited note"
    elif status == "incomplete":
        if reason == NEGATION_REASON:
            line = "Not verified: the answer says nothing is on, but the cited note lists that day"
        elif "entailment unverified" in reason:
            line = "Not verified (no automatic fact-check yet)"
        elif "without citing" in reason:
            line = "Not verified: no source cited"
        elif "never offered" in reason:
            line = "Not verified: it cited a source it wasn't given"
        elif "of the answer's wording" in reason:
            line = "Not verified: the answer strays from its source"
        else:
            line = "Not verified"
    elif status == "withheld":
        line = ("No answer: nothing in the notes matches" if "no evidence" in reason
                else "No answer: the notes don't say")
    elif status == "malformed":
        line = "No usable answer: the model's reply was cut off or empty. Try rephrasing"
    elif status == "unclear":
        line = "No answer: the date wasn't clear"
    elif status == "unavailable":
        line = "The local model isn't answering on the Air. Try again in a minute"
    else:
        line = status
    if any(c.get("unreviewed") for c in citations):
        line += " · from an unreviewed Inbox note"
    return line


# P2.8 B1: the phone shows plain text. Evidence ids, single or grouped ("[E1, E4]"), are
# never shown; nor are markdown tables, bold markers or note paths (D133).
_ASKED = re.compile(r"^\(Asked about (?P<day>[^()]*)\.\) ")
_ID_GROUP = re.compile(r"\[\s*E\s*\d+(?:\s*[,;]\s*E?\s*\d+)*\s*\]", re.I)
# Only the suffix goes: removing a whole path token also took any sentence glued to it
# (B1 review 1, finding 3). A folder name may stay; the brief accepts that.
_MD_SUFFIX = re.compile(r"\.md\b(?::\d+(?:[-–]\d+)?)?", re.I)
_DATED_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[- ]\d{4})?[- ]*")
LATENCY_SHOWN_ABOVE = 8.0  # D56: the latency is news only past the target
TITLE_MAX = 80
QUOTE_WIDTH = 160  # quote_lines' width; the screen never cuts a quote again (B1 review 1)
NO_QUOTE = "    (no line of this note can be shown here)"
# P2.8 B2 (David, Q110): a withheld answer never shows the model's sentence, which is often
# a decline phrase inside a partly right answer ("not specified"); its cited lines do.
NO_CLEAR = "No clear answer. Closest in your notes:"
NOTHING = "Nothing in your notes matches"


def _phone_line(line):
    s = line.strip()
    if s.startswith("|"):  # a table row: its cells; a separator row: nothing
        if set(s) <= set("|-: "):
            return ""
        s = " · ".join(cell.strip() for cell in s.strip("|").split("|") if cell.strip())
    s = re.sub(r"^#+\s*", "", s)
    return re.sub(r"^(?:[-*+•]|\d+[.)])\s+", "", s)


def _guard(s):
    """The screen's promise, by construction: one pass, and no replacement can create
    another forbidden token (B1 review 1, finding 2)."""
    s = s.replace("|", "·")
    s = re.sub(r"\[(?=\s*E)", "(", s, flags=re.I)
    s = re.sub(r"\.(md)", r" \1", s, flags=re.I)  # glued too: ".mdx", ".md_backup"
    return re.sub(r"\*{2,}", "*", s)


def _visible(text):
    """True if a reader would see something: a letter, digit, mark of punctuation or
    symbol, not only spaces or zero-width characters (B1 D74 consult)."""
    return any(unicodedata.category(c)[0] in "LNPS" for c in text or "")


def phone_text(text):
    """Model or note text as one plain line for the phone. Never drops a sentence: lines
    are joined, not filtered, so a sentence the service appended always survives."""
    # Format characters go first, so removing one can never join a forbidden token
    # ("*\u200b*"); splitlines also breaks on U+2028 and the other Unicode line ends.
    text = "".join(c for c in text or "" if unicodedata.category(c) != "Cf")
    lines = [_phone_line(l) for l in text.splitlines()]
    s = " ".join(l for l in lines if l).replace("`", " ")
    for _ in range(10):  # tidy until stable; _guard below is what guarantees the screen
        before = s
        s = _ID_GROUP.sub("", s)
        s = _MD_SUFFIX.sub("", s)
        s = s.replace("**", "").replace("__", "")
        if s == before:
            break
    s = re.sub(r"\s+([.,;:!?])", r"\1", _guard(s))
    return re.sub(r"\s+", " ", s).strip()


def _stems(text):
    return {w[:5] for w in _words(text)}  # "Thursday" and "Thursdays" meet


# Markdown structure lines, never content (B1 review 3, session 50): a table's delimiter
# row with or without outer pipes ("|---|", "--- | :--:", tabs too, and a bare "---"),
# a setext underline ("==="), a thematic break ("***", "_ _ _"), a code fence.
_DELIMITER = re.compile(r"\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*")
_SETEXT = re.compile(r"=+[ \t]*")
_BREAK = re.compile(r"([*_-])(?:[ \t]*\1){2,}[ \t]*")
_FENCE = re.compile(r"(?:`{3,}|~{3,})")


def _is_separator(line):
    """A line that is only markdown structure."""
    return bool(_DELIMITER.fullmatch(line) or _SETEXT.fullmatch(line)
                or _BREAK.fullmatch(line) or _FENCE.match(line))


# B1 review 4 and its D74 consult (session 50): markdown containers and comments. A line's
# container prefixes ("> ", list and task markers) are removed to a fixed point before it is
# classified; HTML and Obsidian comments, tag-only lines, link-reference and footnote
# definitions and callout heads ("[!note]") are structure. Forms outside this set are an
# accepted residual (David, Q117): they may show a structure line; nothing else changes.
_CONTAINER = re.compile(r"(?:>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+|\[[ xX]\][ \t]+)")
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)|%%.*?(?:%%|\Z)", re.S)
_TAG_ONLY = re.compile(r"(?:<[^<>]*>[ \t]*)+")
_DEFINITION = re.compile(r"\[\^?[^\]]+\]:.*|\[![^\]]*\].*")


def _blank_comments(text):
    """Comments become blank lines (line count kept). A "-->" with no opening in the chunk
    closes a comment that began above it: everything up to it is blanked too."""
    head, sep, rest = text.partition("-->")
    if sep and "<!--" not in head:
        text = re.sub(r"[^\n]", "", head) + " " * len(sep) + rest
    return _COMMENT.sub(lambda m: re.sub(r"[^\n]", "", m.group(0)), text)


def _unwrap(line):
    """A line without format characters and container prefixes. Prefixes go one at a
    time, and a thematic break is kept whole ("- - -" is not a list item)."""
    s = "".join(c for c in line if unicodedata.category(c) != "Cf").strip()
    while s and not _BREAK.fullmatch(s):
        m = _CONTAINER.match(s)
        if not m:
            break
        s = s[m.end():].strip()
    return s


def _normalised(line):
    """`_unwrap`ped line, "" if blank, None if it is only structure."""
    s = _unwrap(line)
    if s and (s.startswith("#") or _is_separator(s) or _TAG_ONLY.fullmatch(s)
              or _DEFINITION.fullmatch(s)):
        return None
    return s


def _content_lines(chunk_text):
    """(index, normalised line) for a chunk's content lines: no blank line, heading,
    structure line, comment, leading frontmatter block, nor a line directly above a
    delimiter or underline (a table's header row, a setext heading), all judged after
    `_normalised`. A line holding "|" above a pipe delimiter after blank lines counts as a
    header too, to exclude more."""
    raw = _blank_comments(chunk_text or "").splitlines()
    norm = [_normalised(l) for l in raw]
    skip = set()
    if norm and raw[0].strip() == "---":  # frontmatter at the top of a note's first chunk
        end = next((j for j in range(1, len(raw)) if raw[j].strip() in ("---", "...")), None)
        if end is not None:
            skip.update(range(end + 1))

    def underline(j):
        line = _unwrap(raw[j])
        return bool(_DELIMITER.fullmatch(line) or _SETEXT.fullmatch(line))

    out = []
    for i, line in enumerate(norm):
        if i in skip or not line:
            continue
        if i + 1 < len(norm) and underline(i + 1):
            continue
        later = next((j for j in range(i + 1, len(norm)) if norm[j] != ""), None)
        if "|" in line and later is not None and underline(later) and "|" in raw[later]:
            continue
        out.append((i, line))
    return out


def screen_line(chunk_text, answer_text):
    """The one line of a cited chunk the phone shows under its title (B1; review 2 of
    session 50: the JSON's first quote could be a table's header row and hide the row
    that contradicts the answer). Never a heading, a table separator or a header row,
    which name things rather than state them. The line sharing most words with the
    answer, then most word stems ("Thursday", "Thursdays"), the earliest on a tie; with
    nothing shared, the first body line; "" if no line has anything to show. Cut raw at
    the JSON quote's width before clean-up, so clean-up can never hide its end. The
    JSON's `quote` is not touched."""
    plain = phone_text(answer_text)
    words, stems = _words(plain), _stems(plain)
    best, first = None, ""
    for _, bare in _content_lines(chunk_text):
        shown = phone_text(_cut(bare, QUOTE_WIDTH))
        if not _visible(shown):
            continue
        first = first or shown
        score = (len(_words(shown) & words), len(_stems(shown) & stems))
        if any(score) and (best is None or score > best[0]):
            best = (score, shown)
    return best[1] if best else first


def _stem_title(path):
    name = path.rsplit("/", 1)[-1]
    if name.lower().endswith(".md"):
        name = name[:-3]
    name = _DATED_STEM.sub("", name).replace("-", " ").replace("_", " ")
    return phone_text(name) or "a note"


def note_title(raw, path):
    """The note's first `# ` heading, else its file name made readable."""
    body = raw or ""
    if body.startswith("---"):
        end = body.find("\n---", 3)
        body = body[end + 4:] if end != -1 else ""
    for line in body.splitlines():
        if line.startswith("# "):
            title = phone_text(line[2:])
            if title:
                return _cut(title, TITLE_MAX)
            break
    return _stem_title(path)


def _cut(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _answer_body(payload, question="", day=""):
    """The answer as one plain line, shared by the screen and the spoken form: the
    service's own label for a withheld answer (never model text), else the model's
    answer with the resolved day leading."""
    raw = payload.get("answer") or ""
    asked = _ASKED.match(raw)
    if payload.get("status") == "withheld":
        q = phone_text(question)
        body = (NO_CLEAR if payload.get("citations") else
                (NOTHING + (": “%s”" % q if _visible(q) else ".")))
        label = phone_text(day)
        if _visible(label):
            body = "%s: %s" % (label, body)
        return body
    body = phone_text(raw[asked.end():] if asked else raw)
    if asked:
        day = phone_text(asked["day"])
        short = re.sub(r" \d{4}$", "", day)
        if not body.startswith(short):
            body = "%s: %s" % (day, body) if body else day
    return body


SPOKEN_TITLES = 3  # sources named aloud; the screen lists them all
# The evidence state in words (David, Q149, 21 Oct: words now; a calibrated percentage
# only once T4.1 can back it). Other states say it in their own sentence.
SPOKEN_CUE = {"supported": "Verified.", "incomplete": "Not sure."}


def spoken_text(payload, titles=(), question="", day=""):
    """What the Shortcut speaks (PL1 step 4): the same answer as the screen, then the
    sources by title only, at most SPOKEN_TITLES. No quotes, paths, ids or latency: the
    screen keeps those. Plain text; on-device speech reads it, nothing leaves the Mac."""
    body = _answer_body(payload, question, day)
    if not _visible(body):
        # Nothing to say (a malformed reply): speak the screen's own advice instead.
        body = phone_text(payload.get("status_line") or "") or "No answer"
    out = body.rstrip(" :")
    if out[-1:] not in ".!?":
        out += "."
    cites = payload.get("citations", [])
    cue = SPOKEN_CUE.get(payload.get("status"))
    if cue:
        out += " " + cue
    if any(c.get("unreviewed") for c in cites):
        out += " From an unreviewed note."
    names = []
    for n, c in enumerate(cites[:SPOKEN_TITLES], 1):
        title = phone_text(titles[n - 1] if n <= len(titles) else _stem_title(c.get("path", "")))
        names.append(title if _visible(title) else "a note")
    if names:
        out += " Source%s: %s." % ("s" if len(names) > 1 else "", "; ".join(names))
        if len(cites) > SPOKEN_TITLES:
            out += " And %d more on the screen." % (len(cites) - SPOKEN_TITLES)
    return _guard(out)


def display_text(payload, titles=(), lines_shown=(), question="", day=""):
    """One plain-text block for the Shortcut's Show Result / Speak Text (P2.8 B1): the
    answer, starting with the fact; each source by title with exactly one line from it, so
    a contradiction stays visible (acceptance fix 2, kept by David, Q109): `lines_shown[n]`
    (`screen_line`, which the service always passes; empty means NO_QUOTE, never the JSON
    quote, which may be a table header: B1 review 3), else for a caller passing no lines
    the first showable JSON quote, else NO_QUOTE; one status line; the latency only
    past the target. Paths and line numbers stay in the JSON. A `withheld` answer (B2,
    Q110) shows no model sentence: NO_CLEAR above its sources, or with none cited NOTHING
    and the question; `day`, the service's resolved date label, leads when given. It is
    never read from the answer text, which the model can write (B2 review 1)."""
    cites = payload.get("citations", [])
    body = _answer_body(payload, question, day)
    lines = [body if _visible(body) else "(no answer)"]
    if cites:
        lines.append("")
        for n, c in enumerate(cites, 1):
            title = titles[n - 1] if n <= len(titles) else _stem_title(c.get("path", ""))
            title = phone_text(title)
            lines.append("[%d] %s" % (n, title if _visible(title) else "a note"))
            if n <= len(lines_shown):
                quote = phone_text(lines_shown[n - 1])
            else:
                quote = next((q for q in map(phone_text, c.get("quote") or ()) if _visible(q)), "")
            lines.append("    “%s”" % quote if _visible(quote) else NO_QUOTE)
    lines.append("")
    lines.append(phone_text(payload.get("status_line") or payload.get("status") or ""))
    latency = payload.get("latency_s")
    if isinstance(latency, (int, float)) and latency > LATENCY_SHOWN_ABOVE:
        lines.append("%.1f s" % latency)
    return "\n".join(_guard(line) for line in lines)


def finalise_reply(code, obj):
    """The one place every JSON reply gets its voice (PL1 step 4, D74 consult): on any
    non-200 reply `spoken` is overwritten with the final `display`, never only filled in
    when missing, so an error can never be spoken as anything else. A 200 reply with a
    `display` and no `spoken` (the /wrong replies) speaks its display. /health carries
    neither: no Shortcut speaks it (documented exclusion, tested)."""
    obj = dict(obj)
    if code != 200:
        obj["display"] = obj.get("display") or error_display(code)
        obj["spoken"] = obj["display"]
    elif "display" in obj and "spoken" not in obj:
        obj["spoken"] = obj["display"]
    return obj


def error_display(code):
    """Every non-200 response carries a readable `display` (acceptance fix 4). Generic on
    purpose: it never says *why* a token failed or what the server saw."""
    return {
        400: "The question was empty, too long (500 characters max) or not sent as text.",
        401: "Not authorised: the token in this Shortcut doesn't match. "
             "Re-copy it from the Keychain on the Air (mvp/make-token.sh).",
        403: "Refused: this device isn't on David's Tailscale network.",
        404: "Wrong address: the Shortcut should call /ask.",
        411: "The request had no length; rebuild the Shortcut's request body.",
        413: "The question was too long to send.",
        423: "Paused: a note in the vault is labelled client, or has conflicting labels. "
             "Nothing was answered. The note's path is in the Air's log; remove or relabel it.",
        429: "The brain is still answering a previous question. Try again in a moment.",
        503: "The local model isn't answering on the Air. Try again in a minute.",
        504: "No answer within 2 minutes. The Air may be busy; try a shorter question.",
        500: "Something went wrong on the Air. Nothing was changed; try again.",
    }.get(code, "Request failed (%d)." % code)


def _label(m):
    digits = m.group(1)
    return int(digits) if len(digits) <= 6 else None  # an absurd id is never a source


def _renumber(answer_text, label_to_n):
    """[E3] -> [2]: number sources as the phone lists them; drop ids it does not list."""
    def sub(m):
        n = label_to_n.get(_label(m))
        return "[%d]" % n if n else ""
    return re.sub(r"\s+(?=\[E\d+\])", " ", CITE_ID.sub(sub, answer_text)).replace("  ", " ").strip()


class _Recent:
    """One answer the phone saw. Its text is set only while capture is on and nothing has
    stored the answer yet. The text is read and changed only under the table's lock, held
    for a few lines and never across I/O, so expiry and eviction drop it at once, whatever
    else is running. `lock` orders the automatic capture and a tap: the handler holds it
    from before the reply until its capture is settled, so a tap waits and sees the
    outcome (a failure id, or the text to capture)."""
    __slots__ = ("lock", "answer_id", "wall", "mono", "question", "answer", "status",
                 "status_code", "reason_code", "failure_id", "tapped", "dropped")

    def __init__(self, answer_id, wall, mono):
        self.lock = threading.Lock()
        self.answer_id, self.wall, self.mono = answer_id, wall, mono
        self.question = self.answer = self.status = self.status_code = self.reason_code = None
        self.failure_id = None
        self.tapped = self.dropped = False

    def drop(self):
        self.question = self.answer = None
        self.dropped = True


class RecentAnswers:
    """The in-memory table behind the "wrong" tap (P2.7 step C5, §7a v0.8). Bounded in
    number and in age; nothing here is ever written anywhere. Age is the larger of the wall
    clock's and the monotonic clock's, so neither a clock set back nor time the monotonic
    clock does not count can extend the hold. Every read or change of an entry's text
    happens under `_lock` and checks the age again, so text is never kept, set or handed
    out past the window."""

    def __init__(self, capacity=RECENT_MAX, window=RECENT_WINDOW, clock=time.time,
                 monotonic=time.monotonic):
        self.capacity, self.window = capacity, window
        self.clock, self.monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._items = OrderedDict()

    def __len__(self):
        with self._lock:
            return len(self._items)

    def _live(self, entry):
        """Under _lock: the entry is in the table and inside the window. An entry found
        past the window is dropped here and then."""
        if entry.dropped or self._items.get(entry.answer_id) is not entry:
            return False
        if max(self.clock() - entry.wall, self.monotonic() - entry.mono) >= self.window:
            del self._items[entry.answer_id]
            entry.drop()
            return False
        return True

    def open(self, answer_id):
        """A new entry, returned with its lock held (the caller settles and releases it)."""
        entry = _Recent(answer_id, self.clock(), self.monotonic())
        entry.lock.acquire()
        with self._lock:
            self._items[answer_id] = entry
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)[1].drop()
        return entry

    def settle(self, entry, question, answer, status_code, status, reason_code, failure_id):
        """Record the automatic capture's outcome. The text is kept only if nothing stored
        the answer and the entry is still live."""
        with self._lock:
            entry.status_code, entry.status, entry.reason_code = status_code, status, reason_code
            entry.failure_id = failure_id
            if failure_id is None and self._live(entry):
                entry.question, entry.answer = question, answer

    def get(self, answer_id):
        """The entry for this id if it is still inside the window, else None."""
        with self._lock:
            entry = self._items.get(answer_id)
            return entry if entry is not None and self._live(entry) else None

    def claim(self, entry):
        """A tap, under the entry's lock: 'unknown' if the entry has expired or gone,
        'repeat' if it was tapped, else a snapshot (failure_id, question, answer, status,
        status_code, reason_code). Claiming marks it tapped and drops its text from the
        table; release() undoes that if storing the tap failed."""
        with self._lock:
            if not self._live(entry):
                return "unknown"
            if entry.tapped:
                return "repeat"
            snap = (entry.failure_id, entry.question, entry.answer, entry.status,
                    entry.status_code, entry.reason_code)
            entry.tapped = True
            entry.question = entry.answer = None
            return snap

    def release(self, entry, snap):
        """Storing a claimed tap failed: let the phone try again while the entry is live."""
        with self._lock:
            entry.tapped = False
            if snap[0] is None and self._live(entry):
                entry.question, entry.answer = snap[1], snap[2]

    def sweep(self):
        """Drop every entry at or past the window; returns how many were dropped. Never
        waits for an entry's lock."""
        with self._lock:
            entries = list(self._items.values())
            return sum(1 for e in entries if not self._live(e))

    def start_sweeper(self, interval=RECENT_SWEEP):
        def loop():
            while True:
                time.sleep(interval)
                try:
                    self.sweep()
                except Exception as exc:
                    sys.stderr.write("%s recent-answer sweep failed: %s\n"
                                     % (time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__))
        t = threading.Thread(target=loop, name="dbrain-recent-sweep", daemon=True)
        t.start()
        return t


class CitedNotes:
    """answer id -> the (path, SHA-256) of each note it cited, in the phone's order (PL1
    step 7). Bounded in number and age like RecentAnswers; no note text is held."""

    def __init__(self, capacity=RECENT_MAX, window=RECENT_WINDOW, mono=time.monotonic):
        self.capacity, self.window, self.mono = capacity, window, mono
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def put(self, answer_id, cited):
        with self.lock:
            self.items[answer_id] = (self.mono(), tuple(cited))
            while len(self.items) > self.capacity:
                self.items.popitem(last=False)

    def _sweep(self):
        now = self.mono()
        for k in [k for k, (t, _) in self.items.items() if now - t >= self.window]:
            del self.items[k]

    def sweep(self):
        with self.lock:
            self._sweep()

    def get(self, answer_id):
        with self.lock:
            self._sweep()
            hit = self.items.get(answer_id)
            return hit[1] if hit else None

    def start_sweeper(self, interval=RECENT_SWEEP):
        """Drop expired entries even when no correction comes (review 2/2 should-fix,
        session 59): paths and hashes never outlive the "wrong" hour while the Mac is awake."""
        def run():
            while True:
                time.sleep(interval)
                self.sweep()
        t = threading.Thread(target=run, name="dbrain-cited", daemon=True)
        t.start()
        return t


class _Draft:
    __slots__ = ("mono", "state", "text", "domain", "name", "data", "target")

    def __init__(self, mono, text, domain, name, data, target=None):
        self.mono = mono
        self.state = "pending"            # pending -> saving -> saved (or dropped)
        self.text, self.domain, self.name, self.data = text, domain, name, data
        self.target = target              # step 7: (path, SHA-256) it corrects, else None


class CaptureDrafts:
    """Drafts waiting for "save", in memory only. Each is single use: `claim` moves it to
    `saving` under the lock, so two saves of one id never both write; after the write
    only the outcome is kept (its text and bytes are dropped) so a repeat says "Already
    saved" until the draft would have expired. Expired drafts are dropped on every call."""

    def __init__(self, ttl=DRAFT_TTL, capacity=DRAFT_MAX, mono=time.monotonic):
        self.ttl, self.capacity, self.mono = ttl, capacity, mono
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def __len__(self):
        with self.lock:
            return len(self.items)

    def sweep(self):
        with self.lock:
            self._sweep()

    def start_sweeper(self, interval=RECENT_SWEEP):
        """Drop expired drafts even when no request comes (review 1/2 should-fix): text
        and bytes never outlive DRAFT_TTL + interval while the Mac is awake."""
        def run():
            while True:
                time.sleep(interval)
                self.sweep()
        t = threading.Thread(target=run, name="dbrain-drafts", daemon=True)
        t.start()
        return t

    def _sweep(self):
        now = self.mono()
        for k in [k for k, d in self.items.items() if now - d.mono >= self.ttl]:
            self.items.pop(k).data = None

    def add(self, text, domain, name, data, target=None):
        draft_id = secrets.token_hex(16)
        with self.lock:
            self._sweep()
            while len(self.items) >= self.capacity:
                self.items.popitem(last=False)[1].data = None
            self.items[draft_id] = _Draft(self.mono(), text, domain, name, data, target)
        return draft_id

    def claim(self, draft_id):
        """The draft to write, or an outcome key of SAVE_REPLIES."""
        with self.lock:
            self._sweep()
            d = self.items.get(draft_id)
            if d is None:
                return "unknown"
            if d.state == "saved":
                return "repeat"
            if d.state == "saving":
                return "busy"
            d.state = "saving"
            return d

    def finish(self, draft_id, d, saved):
        with self.lock:
            d.data = d.text = None
            if saved and self.items.get(draft_id) is d:
                d.state = "saved"
            else:
                self.items.pop(draft_id, None)


class Service:
    """Everything the handler needs, so tests can build one with a stub model."""

    def __init__(self, token, model_factory, profile=None, vault=None, limit=None,
                 tracing=True, model_health=None, today=None, failures=None):
        if not token or len(token) < 32:
            raise ValueError("a bearer token of at least 32 characters is required")
        self._token = token.encode("utf-8")
        self.model_factory = model_factory
        self.profile = profile or config.MVP_WHOLE_VAULT
        self.vault = vault
        self.limit = limit or int(os.environ.get("DBRAIN_MVP_CHUNKS", "4"))
        self.tracing = tracing
        self.busy = threading.Lock()
        self.deadline = REQUEST_TIMEOUT
        self.model_health = model_health or (lambda: None)
        self.today = today or today_local
        # P2.7 step C2: a FailureStore, or None (capture off). Set only by main(), and only
        # when the launcher turns capture on.
        self.failures = failures
        # P2.7 step C5: recent answers for a "wrong" tap, in memory only (§7a v0.8).
        self.recent = RecentAnswers()
        # P2.5 F3: David's words -> his notes' wording, appended before retrieval.
        self.aliases = aliases.ALIASES
        # PL1 step 6: drafts for "remember", in memory only.
        self.drafts = CaptureDrafts()
        # PL1 step 7: which notes each recent answer cited, for "that's wrong".
        self.cited = CitedNotes()
        self.now = now_local

    def write_vault(self):
        """Where a saved capture goes: the live vault, never a frozen evidence corpus."""
        return self.vault if self.vault is not None else config.VAULT

    def draft_capture(self, text, domain):
        """(status, reply fields) for POST /capture. A refusal holds nothing."""
        reason, ok = capture.check(text, domain)
        if reason:
            return "refused", reason, {"display": capture.REFUSALS[reason]}
        clean, dom = ok
        name, data = capture.render(clean, dom, self.now())
        draft_id = self.drafts.add(clean, dom, name, data)
        said = capture.read_back(clean, dom)
        shown = "%s\n\nWill be saved as %s/%s (%s, ordinary, approved external, private)." % (
            said, writer.INBOX, name, capture.DOMAINS[dom])
        return "draft", None, {"id": draft_id, "spoken": said, "display": shown}

    def _served(self, path, sha):
        """True if the index the phone's answers use serves `path` with exactly these bytes
        (in scope, label-checked, not already corrected, unchanged since the answer)."""
        try:
            _, files = index.build(self.profile, self.vault, skipped=[])
        except scope.ScopeError:          # ProhibitedNote included: no correction then
            return False
        return bool(sha) and hmac.compare_digest(files.get(path) or "", sha)

    def _reword(self, words, domain):
        """The model's one-sentence version of the dictated correction, or `words` when the
        model is busy, slow, failing, or its text fails the same guards (D143)."""
        if not self.busy.acquire(blocking=False):
            return words
        box = {}

        def work():
            try:
                text, _ = self.model_factory().generate(
                    CORRECT_PROMPT.format(text=words), max_tokens=CORRECT_MAX_TOKENS)
                box["text"] = text
            except Exception:
                pass
            finally:
                self.busy.release()       # only when the model call really ends

        worker = threading.Thread(target=work, name="dbrain-correct", daemon=True)
        worker.start()
        worker.join(CORRECT_MODEL_TIMEOUT)
        got = None if worker.is_alive() else box.get("text")
        if not isinstance(got, str):
            return words
        reason, ok = capture.check(got.strip().strip('"“”'), domain)
        return words if reason else ok[0]

    def draft_correction(self, answer_id, source, text, domain):
        """(status, reason, reply fields) for POST /correct. The target is the answer's
        `source`-th cited note, as the phone listed it; a refusal holds nothing."""
        def refuse(reason):
            msg = capture.REFUSALS.get(reason) or capture.CORRECT_REFUSALS[reason]
            return "refused", reason, {"display": msg, "spoken": msg}
        cited = self.cited.get(answer_id)
        if cited is None:
            return refuse("unknown")
        if not 1 <= source <= len(cited):
            return refuse("source")
        target, sha = cited[source - 1]
        reason, ok = capture.check(text, domain)
        if reason:
            return refuse(reason)
        reason = capture.check_target(target)
        if reason:
            return refuse(reason)
        if not self._served(target, sha):
            return refuse("changed")
        words, dom = ok
        statement = self._reword(words, dom)
        name, data = capture.render_correction(statement, dom, target, self.now())
        if index.correction_target(data.decode("utf-8")) != target:
            return refuse("target")       # the index would not read the target back exactly
        draft_id = self.drafts.add(statement, dom, name, data, target=(target, sha))
        said = capture.read_back_correction(statement, dom, target)
        shown = "%s\n\nWill be saved as %s/%s (%s, ordinary, approved external, private), " \
                "naming %s." % (said, writer.INBOX, name, capture.DOMAINS[dom], target)
        return "draft", None, {"id": draft_id, "spoken": said, "display": shown}

    def save_capture(self, draft_id):
        """(outcome, path or None) for POST /capture/save. The draft's own bytes, checked
        again at save time (admission and guards), written once by the one writer. A
        correction's target must still be served with the bytes the answer cited."""
        d = self.drafts.claim(draft_id)
        if isinstance(d, str):
            return d, None
        saved = False
        try:
            reason, ok = capture.check(d.text, d.domain)
            if reason or ok != (d.text, d.domain):
                return "error", None
            if d.target is not None:
                target, sha = d.target
                if (capture.check_target(target) is not None
                        or index.correction_target(d.data.decode("utf-8")) != target):
                    return "error", None
                if not self._served(target, sha):
                    return "changed", None
            elif index.correction_target(d.data.decode("utf-8")) is not None:
                return "error", None      # a plain capture never names a note to hide
            path = writer.create_inbox_note(self.write_vault(), d.name, d.data)
            saved = True
            return "saved", path
        except writer.TempLeft:
            sys.stderr.write("%s capture save: temporary file left in the Inbox\n"
                             % time.strftime("%Y-%m-%dT%H:%M:%S"))
            return "left", None
        except writer.WriteRefused:
            return "error", None
        except writer.NoteChanged:
            saved = True                  # placed: a repeat must not write a second copy
            sys.stderr.write("%s capture save: placed, not confirmed\n"
                             % time.strftime("%Y-%m-%dT%H:%M:%S"))
            return "check", None
        except Exception as exc:
            sys.stderr.write("%s capture save failed: %s\n"
                             % (time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__))
            return "error", None
        finally:
            self.drafts.finish(draft_id, d, saved)

    def authorised(self, header):
        if not header or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[len("Bearer "):].strip().encode("utf-8"), self._token)

    def titles(self, citations, files):
        """Each cited note's title for the phone (B1). The note is read once more and its
        heading used only if those bytes are the ones this query indexed (`files`: path ->
        SHA-256, after the label check); a note changed since, or unreadable, shows its
        file name, which the JSON already carries (B1 review 1: a reread must not bypass
        the label check). Never fails an answer."""
        out = []
        for c in citations:
            path = c.get("path", "")
            raw = ""
            try:
                # The root index.build used: a frozen profile's own vault when none is set.
                vault = self.vault if self.vault is not None else getattr(self.profile, "vault", None)
                text = scope.read_text(path, self.profile, vault)
                if hmac.compare_digest(hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                       files.get(path) or ""):
                    raw = text
            except (scope.ScopeError, OSError, UnicodeError, ValueError):
                pass
            out.append(note_title(raw, path))
        return out

    @staticmethod
    def _citations(result):
        """Cited sources in the order the phone shows them, each with its quoted line(s),
        and the answer's [En] ids renumbered to match that order. Also returns each
        source's screen line (`screen_line`), in the same order; it never enters the JSON."""
        by_label = {i: c for i, (_, c) in enumerate(result.hits, 1)}
        order, label_to_n = [], {}
        for m in CITE_ID.finditer(result.answer or ""):
            label = _label(m)
            chunk = by_label.get(label)
            if chunk is None or label in label_to_n:
                continue
            cite = chunk.cite()
            existing = [n for n, c in enumerate(order, 1) if c[0] == cite]
            if existing:
                label_to_n[label] = existing[0]
                continue
            order.append((cite, chunk))
            label_to_n[label] = len(order)
        out, shown = [], []
        for cite, chunk in order:
            c = citation(cite)
            c["quote"] = quote_lines(chunk.text, result.answer or "")
            out.append(c)
            shown.append(screen_line(chunk.text, result.answer or ""))
        result.answer = _renumber(result.answer or "", label_to_n)
        return out, shown

    def ask_within(self, question, started, answer_id=None):
        """`ask` under the end-to-end deadline (step 5): `started` is when the request
        arrived (time.monotonic). The caller holds `busy`; the worker releases it when the
        model call really ends, which may be after the phone got its 504."""
        box = {}
        if time.monotonic() - started >= self.deadline:  # already spent reading the request
            self.busy.release()
            return self._timed_out()

        def work():
            try:
                box["result"] = self.ask(question, answer_id=answer_id)
            except Exception as exc:  # never a blank screen on the phone
                sys.stderr.write("%s ask failed: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                         type(exc).__name__))
                box["result"] = (500, {"status": "error"})
            finally:
                self.busy.release()

        worker = threading.Thread(target=work, name="dbrain-ask", daemon=True)
        worker.start()
        worker.join(max(0.0, self.deadline - (time.monotonic() - started)))
        if (worker.is_alive() or "result" not in box
                or time.monotonic() - started > self.deadline):
            return self._timed_out()
        return box["result"]

    def record_failure(self, question, status_code, payload, reason_code=None, answer_id=None):
        """P2.7: keep a failed question for the loop, once per request, at the outcome the
        phone saw, with every signal it raised (failures.SIGNALS); an unverified answer is
        only counted. Never affects the reply: called after it is sent, and any error here
        is reported by its type only."""
        if self.failures is None:
            return None
        try:
            if answer_id is None:
                return self.failures.capture(question, status_code, payload, reason_code)
            return self.failures.capture(question, status_code, payload, reason_code,
                                         answer_id=answer_id)
        except Exception as exc:
            sys.stderr.write("%s failure capture skipped: %s\n"
                             % (time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__))
            return None

    def remember(self, answer_id, question, status_code, payload, reason_code):
        """The capture point (P2.7 steps C2 and C5), called by the handler with the reply
        it is about to send. Returns a callable that sends nothing itself: the handler
        sends the reply, then calls it to capture and settle. While capture is off nothing
        is held: no entry, no text."""
        if self.failures is None:
            return lambda: self.record_failure(question, status_code, payload, reason_code)
        entry = self.recent.open(answer_id)

        def settle():
            fid = None
            try:
                fid = self.record_failure(question, status_code, payload, reason_code,
                                          answer_id=answer_id)
            finally:
                try:
                    self.recent.settle(entry, question, payload.get("answer", ""), status_code,
                                       payload.get("status"), reason_code, fid)
                finally:
                    entry.lock.release()
        return settle

    def mark_wrong(self, answer_id):
        """A tap on the phone: an outcome key of WRONG_REPLIES. The first tap on an answer
        still inside its hour captures it with `user_wrong` (or, if it is stored already,
        adds that signal by one audit line); its text leaves the table when the tap is
        claimed. Nothing else stores anything."""
        if self.failures is None:
            return "off"
        entry = self.recent.get(answer_id)
        if entry is None:
            return "unknown"
        with entry.lock:
            snap = self.recent.claim(entry)       # the age is checked again, under the lock
            if isinstance(snap, str):
                return snap
            fid, question, answer_text, status, status_code, reason_code = snap
            try:
                if fid is not None:
                    if not self.failures.add_signal(fid, WRONG_SIGNAL, answer_id=answer_id):
                        return "unknown"          # deleted since: nothing to mark
                elif question is None:
                    return "unknown"
                else:
                    entry.failure_id = self.failures.capture(
                        question, status_code, {"status": status, "answer": answer_text or ""},
                        reason_code, added=(WRONG_SIGNAL,), answer_id=answer_id)
            except Exception as exc:
                self.recent.release(entry, snap)
                sys.stderr.write("%s wrong tap not recorded: %s\n"
                                 % (time.strftime("%Y-%m-%dT%H:%M:%S"), type(exc).__name__))
                return "error"
            return "recorded"

    def _timed_out(self):
        sys.stderr.write("%s ask exceeded the %g s deadline\n"
                         % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.deadline))
        return 504, {"status": "timeout", "reason": "no answer within %g s" % self.deadline}

    def ask(self, question, answer_id=None):
        started = time.time()
        model = self.model_factory()
        status_code = 200
        today = self.today()
        asked, found = resolve_dates(question, today)
        unclear = [phrase for phrase, days in found if not days]
        if unclear:
            # A date that cannot be read confidently: ask again rather than guess. No
            # retrieval, no model call.
            payload = {"status": "unclear", "reason": "date not understood",
                       "answer": "I couldn't read the date in \"%s\". Ask again with one "
                                 "plain date, such as \"tomorrow\", \"next Monday\" or "
                                 "\"Tue 6 Oct\"." % unclear[0],
                       "citations": [], "latency_s": round(time.time() - started, 2)}
            payload["status_line"] = plain_status("unclear", "", [])
            payload["display"] = display_text(payload)
            payload["spoken"] = spoken_text(payload)
            return 200, payload
        asked = aliases.expand(asked, self.aliases)  # P2.5 F3
        shown, titles = [], []  # display-only (B1); none on the error paths
        label = days_label([d for _, ds in found for d in ds]) if found else ""
        try:
            result = answer.ask(asked, profile=self.profile, model=model,
                                vault=self.vault, limit=self.limit,
                                prompt=dated_prompt(today, found))
            check_dated_negation(result, [d for _, ds in found for d in ds])
            cites, shown = self._citations(result)  # renumbers result.answer: first
            titles = self.titles(cites, result.files)
            if answer_id is not None:     # PL1 step 7: paths and hashes only
                self.cited.put(answer_id, [(c["path"], result.files.get(c["path"]))
                                           for c in cites])
            if found and result.answer:
                # Always state the date the question was resolved to, whatever the answer
                # says (Codex consultation, session 20): it sets the context next to the
                # quoted source lines; it does not certify the answer.
                result.answer = "(Asked about %s.) %s" % (
                    days_label([d for _, ds in found for d in ds]), result.answer)
            payload = {
                "status": result.status,
                "reason": result.reason,
                "answer": result.answer,
                "citations": cites,
                # For the capture point only: the handler removes it before the reply, so
                # the phone's JSON is unchanged (P2.7 step C2).
                REASON_KEY: result.reason_code,
            }
        except index.ProhibitedNote as exc:
            # AGENTS.md rules 2-3: client material (or conflicting labels) is a breach to stop
            # on and report, not to skip. Every question is refused until it is resolved;
            # the path goes to the Air's log for David, never the note's content.
            sys.stderr.write("%s POLICY FLAG: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), exc))
            result = None
            status_code = 423
            payload = {"status": "paused", "reason": "prohibited note in the vault",
                       "answer": "", "citations": []}
        except adapter_mod.MalformedResponse as exc:
            result = getattr(exc, "partial", None)
            payload = {"status": "malformed", "reason": "the local model's reply could not be used",
                       "answer": "", "citations": []}
        except adapter_mod.ModelUnavailable as exc:
            result = getattr(exc, "partial", None)
            status_code = 503
            payload = {"status": "unavailable", "reason": "the local model is not answering",
                       "answer": "", "citations": []}
        payload["latency_s"] = round(time.time() - started, 2)
        payload["status_line"] = plain_status(payload["status"], payload["reason"],
                                              payload["citations"])
        payload["display"] = display_text(payload, titles, shown, question, label)
        payload["spoken"] = spoken_text(payload, titles, question, label)
        if self.tracing and result is not None:
            try:
                trace.write(result, model, self.profile, answer_id=answer_id)
            except outpath.OutsideVaultError:
                pass  # the guard refused; the answer is still returned, nothing is written
        return status_code, payload


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "dbrain-ask/0.1"
        sys_version = ""
        timeout = REQUEST_TIMEOUT + 10

        # Logging and error replies carry only server-generated text (Codex consultation,
        # session 20): a fixed method label (GET, POST, OTHER), a fixed route label (/ask,
        # /health, OTHER), a numeric status and fixed event names. Nothing the client sent
        # (method, path, query, headers, body) is ever interpolated, so the token cannot
        # reach the log whichever field it is put in.
        def _labels(self):
            method = getattr(self, "command", None)
            method = method if method in ("GET", "POST") else "OTHER"
            path = (getattr(self, "path", "") or "").split("?", 1)[0]
            route = path if path in ROUTES else "OTHER"
            return method, route

        def _log(self, event, code=None):
            method, route = self._labels()
            try:
                code = "%d" % int(getattr(code, "value", code))
            except (TypeError, ValueError):
                code = "-"
            sys.stderr.write("%s %s %s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                  method, route, event, code))

        def log_request(self, code="-", size="-"):
            self._log("response", code)

        def log_error(self, fmt, *args):
            self._log("error")  # http.server's messages can quote the request line

        def log_message(self, fmt, *args):
            self._log("message")

        def send_error(self, code, message=None, explain=None):
            """http.server's own errors (bad request line, unsupported method, header
            limits) would echo client text in the status line and the body. Both supplied
            strings are discarded: the reply is the fixed reason phrase and `display`."""
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = 500
            self.close_connection = True
            body = json.dumps(finalise_reply(code, {"status": "error"})).encode("utf-8")
            self.send_response(code)  # reason phrase from http.server's fixed table
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)

        # The end-to-end deadline runs from when the connection opened, so header reading
        # counts too. A connection whose headers are not complete by then is closed without
        # a reply (there is no request yet to answer); one with headers gets 504.
        def setup(self):
            self.arrived = time.monotonic()
            self.headers_done = threading.Event()
            self.dropped = False
            super().setup()
            self._header_timer = threading.Timer(service.deadline, self._drop_if_no_headers)
            self._header_timer.daemon = True
            self._header_timer.start()

        def _drop_if_no_headers(self):
            if not self.headers_done.is_set():
                self.dropped = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def parse_request(self):
            ok = super().parse_request()
            self.headers_done.set()
            self._header_timer.cancel()
            if self.dropped:  # the deadline closed the connection mid-headers: no reply
                self.close_connection = True
                return False
            return ok

        def finish(self):
            self._header_timer.cancel()
            super().finish()

        def _send(self, code, obj):
            obj = finalise_reply(code, obj)
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self, length, started):
            """The body, read under what is left of the end-to-end deadline; None if it
            runs out. Each read waits at most the time remaining, so a slow sender cannot
            hold the request past the deadline."""
            settimeout = getattr(self.connection, "settimeout", None)
            buf = b""
            try:
                while len(buf) < length:
                    left = service.deadline - (time.monotonic() - started)
                    if left <= 0:
                        return None
                    if settimeout:
                        settimeout(left)
                    chunk = self.rfile.read1(length - len(buf))
                    if not chunk:
                        break  # short body: the JSON check answers 400
                    buf += chunk
            except (socket.timeout, TimeoutError):
                return None
            finally:
                if settimeout:
                    settimeout(self.timeout)
            if time.monotonic() - started >= service.deadline:
                return None
            return buf

        def _peer_ok(self):
            if allowed_address(self.client_address[0]):
                return True
            self._send(403, {"status": "refused", "reason": "not on the tailnet"})
            return False

        def do_GET(self):
            if not self._peer_ok():
                return
            if self.path != "/health":
                return self._send(404, {"status": "not found"})
            self._send(200, {"ok": True, "model": service.model_health(),
                             "busy": service.busy.locked()})

        def _authorised_body(self, started, display=None):
            """The checks every POST route shares: token, length, body under the deadline.
            The raw body, or None once a refusal has been sent. `display` replaces the
            question-worded 411/413 text on the capture routes."""
            if not service.authorised(self.headers.get("Authorization")):
                self._send(401, {"status": "unauthorised"})
                return None
            extra = {"display": display} if display else {}
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(411, dict({"status": "length required"}, **extra))
                return None
            if length <= 0 or length > MAX_BODY:
                self._send(413, dict({"status": "too large", "reason": "max %d bytes" % MAX_BODY},
                                     **extra))
                return None
            raw = self._read_body(length, started)
            if raw is None:  # the deadline runs from arrival, body reading included
                self._send(*service._timed_out())
            return raw

        def _wrong(self, started):
            """POST /wrong {"id": "<answer_id>"} (P2.7 step C5). Only that one key, only an
            id of the form /ask hands out; anything else is refused with a fixed reply and
            nothing is stored or logged from it."""
            raw = self._authorised_body(started)
            if raw is None:
                return
            try:
                body = json.loads(raw.decode("utf-8"))
                if not (isinstance(body, dict) and set(body) == {"id"}
                        and isinstance(body["id"], str) and ANSWER_ID.fullmatch(body["id"])):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._log("wrong-refused")
                return self._send(400, {"status": "bad request",
                                        "reason": 'send JSON: {"id": "<answer_id>"}'})
            outcome = service.mark_wrong(body["id"])
            self._log("wrong-" + outcome)
            self._send(200, {"status": outcome, "display": WRONG_REPLIES[outcome]})

        def _capture(self, started):
            """POST /capture {"text": "...", "domain": "work" | "personal"} (PL1 step 6).
            Exactly those two keys, both text. Never calls the model, never takes the
            question lock, never traces or captures a failure; the log gets a fixed event
            with the refusal class only."""
            raw = self._authorised_body(started, CAPTURE_BAD_REQUEST)
            if raw is None:
                return
            try:
                body = json.loads(raw.decode("utf-8"))
                if not (isinstance(body, dict) and set(body) == {"text", "domain"}
                        and isinstance(body["text"], str) and isinstance(body["domain"], str)
                        and len(body["domain"]) <= MAX_DOMAIN):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._log("capture-bad-request")
                return self._send(400, {"status": "bad request", "display": CAPTURE_BAD_REQUEST})
            status, reason, fields = service.draft_capture(body["text"], body["domain"])
            self._log("capture-" + (("refused-" + reason) if reason else status))
            self._send(200, dict(fields, status=status))

        def _correct(self, started):
            """POST /correct {"answer_id": "...", "source": "1".."9", "text": "...",
            "domain": "work" | "personal"} (PL1 step 7, D143). Exactly those keys. Drafts one
            correction note and reads it back; the save goes through /capture/save. The log
            gets a fixed event with the refusal class only."""
            raw = self._authorised_body(started, CAPTURE_BAD_REQUEST)
            if raw is None:
                return
            try:
                body = json.loads(raw.decode("utf-8"))
                if not (isinstance(body, dict)
                        and set(body) == {"answer_id", "source", "text", "domain"}
                        and isinstance(body["answer_id"], str)
                        and ANSWER_ID.fullmatch(body["answer_id"])
                        and isinstance(body["source"], (str, int))
                        and not isinstance(body["source"], bool)
                        and CORRECT_SOURCE.fullmatch(str(body["source"]).strip())
                        and isinstance(body["text"], str) and isinstance(body["domain"], str)
                        and len(body["domain"]) <= MAX_DOMAIN):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                self._log("correct-bad-request")
                return self._send(400, {"status": "bad request", "display": CAPTURE_BAD_REQUEST})
            status, reason, fields = service.draft_correction(
                body["answer_id"], int(str(body["source"]).strip()), body["text"], body["domain"])
            self._log("correct-" + (("refused-" + reason) if reason else status))
            self._send(200, dict(fields, status=status))

        def _capture_save(self, started):
            """POST /capture/save {"id": "<draft id>"}: write that draft, once."""
            raw = self._authorised_body(started, CAPTURE_BAD_REQUEST)
            if raw is None:
                return
            try:
                body = json.loads(raw.decode("utf-8"))
                if not (isinstance(body, dict) and set(body) == {"id"}
                        and isinstance(body["id"], str) and DRAFT_ID.fullmatch(body["id"])):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                # The Shortcut sends an empty id after a refusal: a plain answer, not an error.
                self._log("save-bad-request")
                return self._send(200, {"status": "none", "display": SAVE_REPLIES["none"]})
            outcome, path = service.save_capture(body["id"])
            self._log("save-" + outcome)
            if outcome == "saved":
                return self._send(200, {"status": "saved", "path": path,
                                        "spoken": "Saved to your Inbox.",
                                        "display": "Saved to your Inbox: %s" % path})
            self._send(200, {"status": outcome, "display": SAVE_REPLIES[outcome]})

        def do_POST(self):
            started = self.arrived
            if not self._peer_ok():
                return
            if self.path == "/wrong":
                return self._wrong(started)
            if self.path == "/capture":
                return self._capture(started)
            if self.path == "/capture/save":
                return self._capture_save(started)
            if self.path == "/correct":
                return self._correct(started)
            if self.path != "/ask":
                return self._send(404, {"status": "not found"})
            raw = self._authorised_body(started)
            if raw is None:
                return
            try:
                body = json.loads(raw.decode("utf-8"))
                question = body["question"]
                if not isinstance(question, str):
                    raise TypeError
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                return self._send(400, {"status": "bad request",
                                        "reason": 'send JSON: {"question": "..."}'})
            question = question.strip()
            if not question or "\x00" in question or len(question) > MAX_QUESTION:
                return self._send(400, {"status": "bad request",
                                        "reason": "question must be 1-%d characters" % MAX_QUESTION})
            if not service.busy.acquire(blocking=False):
                return self._send(429, {"status": "busy",
                                        "reason": "one question at a time; try again shortly"})
            # A random id per answer (P2.7 step C5), not derived from the question: the
            # phone sends it back to mark this answer wrong; the trace records it too.
            answer_id = secrets.token_hex(16)
            code, payload = service.ask_within(question, started, answer_id)
            payload = dict(payload)
            reason_code = payload.pop(REASON_KEY, None)
            if code != 200:
                # The error text replaces both, so the voice never says "No answer." over
                # an actionable error (PL1 step 4 review 1/1, session 54).
                payload = dict(payload, display=error_display(code), spoken=error_display(code))
            payload["answer_id"] = answer_id
            # Once per request, here and only here, from the outcome the phone sees: a model
            # worker that finishes after a 504 records nothing more (P2.7 steps C2, C5).
            # The failure store gets exactly what it got before the voice existed: `spoken`
            # is derived text for the phone, not a field the store was reviewed for (D108).
            settle = service.remember(answer_id, question, code,
                                      {k: v for k, v in payload.items() if k != "spoken"},
                                      reason_code)
            try:
                self._send(code, payload)
            finally:
                settle()

    return Handler


def llama_health(endpoint=None):
    """`up` or `down`. The same guards as inference: the endpoint must be loopback and
    resolve to it, the request goes to the pinned address, and a redirect is refused."""
    try:
        model = adapter_mod.LlamaCppAdapter(endpoint=endpoint, timeout=3)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             adapter_mod.RefuseRedirects())
        req = urllib.request.Request(model.endpoint + "/health",
                                     headers={"Host": model.host_header})
        with opener.open(req, timeout=3) as r:
            return "up" if r.status == 200 else "down"
    except Exception:
        return "down"


class _Server(ThreadingHTTPServer):
    # A timeout on the listening socket, so accept() never blocks the serving loop: if the
    # selector reports a connection that is gone by the time accept() runs, the loop gets a
    # timeout (an OSError the stdlib already swallows), goes back to polling, and still sees
    # shutdown(). Without it, shutdown() could wait for ever: the suspected cause of the
    # intermittent P2.4 test hang, not proven (Codex consultation, 8 Oct). Accepted connections get their own timeout in Handler.setup().
    ACCEPT_TIMEOUT = 0.5

    def server_activate(self):
        super().server_activate()
        self.socket.settimeout(self.ACCEPT_TIMEOUT)

    def handle_error(self, request, client_address):
        # No traceback: a fixed event only (a dropped connection, a client gone mid-reply).
        sys.stderr.write("%s connection error\n" % time.strftime("%Y-%m-%dT%H:%M:%S"))


def build_server(host, port, service):
    check_bind(host)
    return _Server((host, port), make_handler(service))


def setup_failures(service, env, store_factory=None, store_path=None):
    """P2.7: capture is off unless the launcher turns it on (D110; the loop's go). Retention
    is not tied to that switch: §7a's 90 days apply to every stored failure, so the sweep
    runs whenever the store exists, even after capture is turned off again. Returns the
    store whose retention was started, or None."""
    store_factory = store_factory or failures_mod.FailureStore
    store_path = Path(store_path or failures_mod.STORE)
    capture_on = env.get("DBRAIN_FAILURE_CAPTURE") == "on"
    if not (capture_on or store_path.exists()):
        return None
    store = store_factory()
    store.start_retention()
    service.failures = store if capture_on else None
    if capture_on:
        service.recent.start_sweeper()   # §7a v0.8: no recent answer outlives RECENT_HOLD
    return store


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dbrain.serve", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=None, help="Tailscale address (default: from the Tailscale CLI)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)

    try:
        today_local()
    except Exception:
        raise SystemExit("no time-zone database for %s: refusing to start rather than guess "
                         "today's date" % TIMEZONE)
    host = args.host or tailscale_ipv4()
    if not host:
        raise SystemExit("no Tailscale address found: install Tailscale and log in, or pass --host")
    try:
        check_bind(host)
    except BindRefused as exc:
        raise SystemExit(str(exc))
    bearer = keychain_token()
    service = Service(
        bearer,
        model_factory=lambda: adapter_mod.LlamaCppAdapter(timeout=REQUEST_TIMEOUT - 10),
        model_health=llama_health,
    )
    del bearer
    setup_failures(service, os.environ)
    service.drafts.start_sweeper()
    service.cited.start_sweeper()
    server = build_server(host, args.port, service)
    sys.stderr.write("%s dbrain-ask listening on http://%s:%d (profile %s)\n"
                     % (time.strftime("%Y-%m-%dT%H:%M:%S"), host, args.port, service.profile.name))
    server.serve_forever()


if __name__ == "__main__":
    main()
