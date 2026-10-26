# MVP-phone: ask the second brain from the iPhone

Brief: `90 System/briefs/MVP-phone.md` in the vault (David's Q32–Q34). On `main`.
Read-only: the service reads the whole vault minus dot-folders and minus the system's own
working notes (P2.8, D133): `90 System/`, `10 Projects/AI Second Brain/`, the agents'
orchestration record in `40 Archive/`, `AGENTS.md`, `CLAUDE.md`, `index.md` and `log.md`
(`config.SYSTEM_NOTES`). They quote test questions word for word and outranked the notes the
questions are about. Everything else, all of `00 Inbox/` included, stays in. A note with no
`classification`, an unrecognised one (`restricted`) or frontmatter it cannot verify is
skipped and traced, never indexed; the other notes still answer. A note with a recognisable
`client` label, or conflicting labels, is not skipped: it **pauses** the service (every
question gets 423 "Paused", the path goes to this Mac's log as `POLICY FLAG`, nothing is
answered) until it is removed or relabelled. Answers come from the local model on this Mac; the
service writes only traces and logs, to `~/Library/Application Support/dbrain/logs/`
(outside the vault and outside git; data policy §7b).

Dates: one plain date per question is read (today, tomorrow, the day after tomorrow,
yesterday, a weekday with this/next/on, this or next weekend, this or next week, an explicit
date such as "Tue 6 Oct"; English and French). Any other date-like text makes the reply
"I couldn't read the date…", with no answer, rather than an answer about the wrong day.
That includes a month on its own ("November lunches?"): ask with a day, or without the month.

```
iPhone Shortcut ── Tailscale (WireGuard) ──▶ this Air: dbrain.serve :8765 ──▶ query service ──▶ llama-server 127.0.0.1:18087
```

## One-time setup (David)

1. **Tailscale** on the Air and the iPhone, same account; turn on MagicDNS in the admin
   console. Note the Air's name (e.g. `my-mac`) and tailnet suffix (`….ts.net`).
2. **Energy:** System Settings › Battery › Options: prevent automatic sleeping on power
   adapter when the display is off. Keep the lid open (a closed lid sleeps the Air unless
   an external display is attached).
3. **Token:** in Terminal, `cd ~/Work/dbrain-runtime && ./mvp/make-token.sh`. It goes
   into the Keychain and onto the clipboard for 2 minutes. Paste it straight into the
   Shortcut (step 6). It is never stored in a file.
4. **Model files:** `./mvp/prepare-model.sh` (installs the pinned runtime from its archive, hash-checked, if it
   is not already in `~/Library/Application Support/dbrain/`; checks the hashes of the default model, Gemma 4 26B-A4B
   QAT Q4_0, 14.4 GB, chosen by T9c and adopted 21 Oct (Q152), fetched under `artifacts/t9c-2026-10-21/`, and of the
   fallback Gemma 4 E4B QAT Q4_0, 5.15 GB (T9, 8 Oct); this script never downloads a model). The 26B-A4B holds about
   15 GiB while the service runs; to go back to E4B, swap the two `MODEL`/`MODEL_PIN` pairs in `run-all.sh` and restart.
5. **Start:** double-click `mvp/Ask Brain.command`. To start at login, add that file in
   System Settings › General › Login Items. Keep its Terminal window open. It restarts the
   model or the service if either stops, and keeps the Air awake on power while it runs.
   (A launchd variant would need Full Disk Access for the system Python; see the queue.)
6. **Shortcut "Ask Brain":** Cowork writes the step-by-step. In short:
   - Ask for Input (text; dictation works from the keyboard).
   - Get Contents of URL: `http://<air-name>.<tailnet>.ts.net:8765/ask`, method POST,
     header `Authorization: Bearer <token>`, request body JSON with key `question`.
   - Get Dictionary Value `display` › Show Result (optionally Speak Text). Since P2.8 the
     `display` is plain text: the answer, each source as its note's title with one quoted
     line, one status line, and the time only past 8 s. Paths and line numbers stay in the
     JSON (`citations`). A withheld answer shows no model sentence: "No clear answer.
     Closest in your notes:" above its sources, or "Nothing in your notes matches" and the
     question when nothing was cited (P2.8 B2).
   - `GET …:8765/health` (no token) reports whether the service and the model are up.
     With the Air asleep nothing answers and the Shortcut hangs: Shortcuts has no client
     timeout, and David accepted this on the Air host (Q36).

## Marking an answer wrong (P2.7 step C5)
Every `/ask` reply carries `answer_id`, a random id (not derived from the question). While
failure capture is on, the service keeps each recent question and its answer **in memory
only, never on disk, for at most one hour** (data policy §7a, v0.8; D123): a tap is accepted
for 59¾ minutes, and a sweep every 15 s drops anything older; a restart drops everything.
`POST /wrong` with the same token and the body `{"id": "<answer_id>"}` (nothing else is
accepted) stores that answer as a failure with the signal `user_wrong`, once; if it was
stored already for another reason, the audit log gets one text-free line adding the signal.
The reply's `display` says what happened (marked, already marked, too late, switched off).
With capture off nothing is held and a tap stores nothing.

**Shortcut change (David edits "Ask Brain" himself; about 2 minutes):**
1. After **Get Contents of URL** (the `/ask` call), add **Get Dictionary Value**: key
   `answer_id`, from *Contents of URL*. Then **Set Variable** `AnswerID` to it.
2. In the existing **Get Dictionary Value** `display`, tap its input and choose the first
   **Contents of URL** (the `/ask` result), not `AnswerID`; keep **Show Result** after it.
3. Add **Choose from Menu**, prompt `Is this answer right?`, two items: `OK`, `Wrong`.
4. Under `OK`: nothing (or **Stop This Shortcut**).
5. Under `Wrong`: **Get Contents of URL** `http://<air-name>.<tailnet>.ts.net:8765/wrong`,
   method POST, header `Authorization: Bearer <token>` (the same token as for `/ask`),
   request body JSON with one key, `id`, of type Text, value the variable `AnswerID`.
6. Still under `Wrong`: **Get Dictionary Value** `display` from that result › **Show Result**.
7. Leave **End Menu** where Shortcuts puts it. Nothing else changes.

## Remembering from the phone (PL1 step 6)
"Brain Remember" (`./mvp/make-voice-shortcut.sh --remember`; the lead builds it, it calls
Brain Key for the token) asks **Work or personal?**, takes dictation, and sends
`POST /capture {"text": ..., "domain": ...}`. The service either refuses with a spoken
reason and keeps nothing, or freezes the exact note it would write and reads it back
(spoken, and on screen with the file name and labels). Only **Save** sends
`POST /capture/save {"id": ...}`, which writes that note once as a new file in `00 Inbox/`;
Cancel, or no answer within 5 minutes, leaves nothing. Your read-back is the gate (Q154);
the word lists in `dbrain/capture.py` refuse what they detect (secrets, long numbers,
client or engagement words, children, health, finances, journal) and cannot see implicit
meaning. Personal notes are refused until P1.4 is applied and the lint check passes
(Q153, Q156). Captures never reach the model, the traces or the failure store, and the log
records only a fixed event (`capture-draft`, `capture-refused-<class>`, `save-saved`, ...).
Tests: `tests/test_capture.py`, `tests/test_writer.py`.

## Correcting from the phone (PL1 step 7, append-only, D143)
After an answer, "Ask Brain" (and "Ask Brain Text") offer **OK / Wrong / Correct**.
**Correct** asks which source number (1 by default), **Work or personal?**, and what is
correct, then sends `POST /correct {"answer_id", "source", "text", "domain"}`. The phone
never sends a path: the service kept each recent answer's cited notes (paths and hashes
only, in memory, for the "wrong" hour). The model rewords the correction once (skipped
when busy or slow; anything the word lists refuse falls back to your own words), and the
service reads back the note it would add **and the note that will leave answers**. **Save**
goes through `POST /capture/save`: the one writer adds one new Inbox note with
`note_type: correction` and `corrects: <path>`; from the next question the index leaves the
named note out of answers. No existing file is edited, so undoing a correction is deleting
that Inbox note. Refused, and nothing kept: an answer over an hour old, a source number it
did not have, a note that changed since the answer or is not served, a path whose words or
shape the guards refuse, and everything step 6 refuses. The whole named note leaves answers,
not just the wrong line (accepted in D143). Tests: `tests/test_correct.py`.

## What the service guarantees (tested in `tests/test_serve.py`)
- Listens only on the Tailscale address (or loopback); refuses `0.0.0.0`, `::` and LAN
  addresses, and refuses any peer outside the tailnet.
- Bearer token, constant-time comparison, 401 otherwise; the token is never logged,
  traced or returned.
- One question at a time (429 otherwise), 4 KB body, 500-character question.
- No cloud fallback: if the local model is down the answer is 503 "unavailable".
- Status is `incomplete`, `withheld`, `unclear` (a date it could not read: no answer, asks again), `malformed` or `unavailable`; never `supported`
  before T4.1 (Q21). Citations show their folder; `00 Inbox` ones are marked unreviewed.
- `/ask` changes nothing in the vault; only a Save after a read-back adds one new Inbox
  note (steps 6 and 7).

## Traffic and data flow (for T8)
Content travels only between David's devices, encrypted by WireGuard. Tailscale's
coordination server sees device metadata (names, keys, addresses, connection times), not
questions or answers. The hop is plain HTTP inside the WireGuard tunnel; if the Shortcut
refuses `http://`, `tailscale serve` can put HTTPS in front with a tailnet certificate.

## Logs and traces
In `~/Library/Application Support/dbrain/logs/` (`DBRAIN_LOGS` overrides it; refused
inside the vault or a git working tree): service and model logs (method, path, status;
never the question or token; the model server's log was checked with
`experiments/server_log_check.py`) and `traces-YYYY-MM-DD.jsonl` (question as a SHA-256,
paths read, timings, status, `reason_code`). Logs and traces written before 9 Oct are in
the runtime's git-ignored `artifacts/` folder.
