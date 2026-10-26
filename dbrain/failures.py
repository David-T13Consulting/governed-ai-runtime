"""P2.7 stage C: the failure store and its audit log (data policy §7a and §7b).

When a question raised a failure signal, the question and its answer are kept in a local
store outside the vault and outside git, readable only by the local model and by David
(§7a; agents are kept out by D108/D110). Every step is recorded in an append-only audit
log that never holds question or answer text (§7b), so the loop can be audited without
reading the store. Failures are deleted after 90 days by a retention thread that runs
whenever the store exists, whether or not new capture is on, and each deletion is recorded.

Every write, listing and deletion goes through outpath's anchored walk (no symlink at any
depth, not in the vault, not in a git working tree, private to this user, and the path
resolved once at construction). Records are written to a temporary name and renamed into
place, so a record is either complete or absent. The audit log records intent before each
action and completion after it; `reconcile()` completes any capture or deletion whose
completion record was lost, from what is actually in the store.

Capture is off unless the service is given a store (`DBRAIN_FAILURE_CAPTURE=on` in the
launcher). The clock is injectable so retention is tested without waiting 90 days.
"""
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import answer, outpath

APP = Path.home() / "Library" / "Application Support" / "dbrain"
STORE = APP / "failures"    # question text: the local model and David only (§7a)
LOGS = APP / "logs"         # text-free logs, readable for audit (§7b)
AUDIT_NAME = "failure-audit.jsonl"
RETENTION_DAYS = 90
RETENTION_INTERVAL = 6 * 3600   # seconds between sweeps while the service runs
TMP_PREFIX = ".tmp-"            # a record being written; never a stored failure
TMP_MAX_AGE = 3600              # a temporary file older than this is a leftover: removed
LOCK_NAME = ".lock"             # flock file: capture, reconcile and purge never interleave

# The one table of failure signals (P2.7 step C2, D113): which final outcome the phone saw
# counts as a failure. A request may raise more than one; it is still captured once, with
# all of them. `user_wrong` is the phone's one-tap "wrong" (step C5): never raised by an
# outcome itself, only added by the service when David taps (serve.Service.mark_wrong).
SIGNALS = {
    "withheld": "no answer: the model declined, or nothing was retrieved",
    # The date reader fails closed: any date-like text it cannot read fully makes the
    # question `unclear` (serve.resolve_dates, _leftover), so "no date read on a date-like
    # question" is this signal.
    "unclear": "a date-like question whose date was not read",
    "timeout": "no answer within the deadline (504)",
    "unavailable": "the local model is down (503)",
    "malformed": "the model's reply could not be used",
    "incomplete": "a defect: an id never offered, no citation, grounding below threshold, "
                  "or a verifier rejection (answer.DEFECTS)",
    "user_wrong": "David marked the answer wrong on the phone (one tap, step C5)",
}
# Cited and grounded, but no verifier yet: not a failure. Counted in the audit log, never
# stored (D113).
UNVERIFIED = answer.UNVERIFIED

# delete(): a stored failure's id as capture() makes it, and a reason from a fixed list
# (never caller text: the reason goes to the audit log, which must hold no question text).
_ID = re.compile(r"[0-9]{1,12}-[0-9a-f]{8}")
# The random id the phone got with an answer (step C5; serve.ANSWER_ID): text-free, so it
# may go to the audit log, where it ties a failure to its trace line.
_ANSWER_ID = re.compile(r"[0-9a-f]{32}")
DELETE_REASONS = {
    "planted-test": "a planted test record, removed after the check it served (C4)",
    "david-request": "David chose to drop this failure before its 90 days",
}

_GUARD = {"outside_git": True, "private": True, "strict": True}
_AUDIT_LOCK = threading.Lock()   # in-process; flock serialises across processes
_O_RD = os.O_RDONLY | outpath.O_NOFOLLOW
_O_RD_RECORD = _O_RD | os.O_NONBLOCK   # a FIFO planted as a record must not block the sweep


def _report(event, fid, exc):
    """Last resort when an error cannot even be written to the audit log: one text-free
    line on stderr (event, id, error type), so the failure is never silent."""
    try:
        sys.stderr.write("%s failure %s %s not audited: %s\n"
                         % (time.strftime("%Y-%m-%dT%H:%M:%S"), event, fid, type(exc).__name__))
        sys.stderr.flush()
    except Exception:
        pass


def _sync(fd):
    """Make what was written through fd durable (F_FULLFSYNC where macOS has it)."""
    try:
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    except (AttributeError, OSError):
        os.fsync(fd)


def signals_for(status_code, payload, reason_code=None):
    """The failure signals of a final outcome, sorted; empty for none. A 504 is a timeout
    and a 503 a model outage whatever their body. `incomplete` is a signal only for a
    defect: an unverified answer raises none, and an `incomplete` with no known code is
    treated as a defect (every verdict carries a code since C1b; a missing one is a bug
    the loop should see)."""
    status = payload.get("status") if isinstance(payload, dict) else None
    found = set()
    if status_code == 504 or status == "timeout":
        found.add("timeout")
    if status_code == 503 or status == "unavailable":
        found.add("unavailable")
    if status in ("withheld", "unclear", "malformed"):
        found.add(status)
    if status == "incomplete" and reason_code != UNVERIFIED:
        found.add("incomplete")
    return sorted(found)


def _check_delete(fid, reason):
    """delete()'s arguments: an id of capture()'s form and a reason from DELETE_REASONS."""
    if not (isinstance(fid, str) and _ID.fullmatch(fid)):
        raise ValueError("not a failure id")
    if not (isinstance(reason, str) and reason in DELETE_REASONS):
        raise ValueError("reason: one of %s" % ", ".join(sorted(DELETE_REASONS)))


def _check_answer_id(answer_id):
    if answer_id is not None and not (isinstance(answer_id, str)
                                      and _ANSWER_ID.fullmatch(answer_id)):
        raise ValueError("not an answer id")


def _digest(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_record(record, fid):
    """A complete stored failure: what capture() writes, with this id."""
    return (isinstance(record, dict) and record.get("id") == fid
            and _finite_number(record.get("ts"))
            and isinstance(record.get("signals"), list) and record["signals"]
            and all(isinstance(x, str) and x in SIGNALS for x in record["signals"])
            and isinstance(record.get("status_code"), int)
            and not isinstance(record.get("status_code"), bool)
            and "status" in record
            and (record["status"] is None or isinstance(record["status"], str))
            and isinstance(record.get("question"), str) and isinstance(record.get("answer"), str))


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class FailureStore:
    """The store (one file per failure) and the audit log."""

    def __init__(self, store=None, logs=None, clock=time.time):
        # Resolved once, here. Every later write, listing and deletion uses these real paths
        # through outpath's no-follow walk, so a symlink swapped in afterwards for the store,
        # the logs folder or any ancestor is refused, never followed.
        checked = []
        for given, default in ((store, STORE), (logs, LOGS)):
            path = Path(given or default)
            real = outpath.assert_outside_git(path)
            # The folder itself must not be a symlink (it would carry the store away from the
            # folder the D108/D110 deny rules protect). The production default must not be
            # redirected at all.
            if real != Path(os.path.realpath(str(path.parent))) / path.name or \
                    (given is None and real != Path(os.path.abspath(str(path)))):
                raise outpath.OutsideVaultError("refusing %s: it is redirected (a symlink?)" % path)
            checked.append(real)
        # The validated paths themselves are kept: never resolved again, so a symlink made
        # after the check cannot become the destination (strict I/O refuses it instead).
        self.store, self.logs = checked
        self.clock = clock
        self._lock = threading.Lock()

    @contextmanager
    def _store_lock(self):
        """One capture, reconcile or purge at a time: a thread lock in this process and an
        exclusive flock on the store's lock file for other processes."""
        with self._lock:
            fd = outpath.open_dir_guarded(self.store, **_GUARD)
            try:
                lf = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | outpath.O_NOFOLLOW, 0o600,
                             dir_fd=fd)
            finally:
                os.close(fd)
            try:
                fcntl.flock(lf, fcntl.LOCK_EX)
                yield
            finally:
                os.close(lf)

    # -- audit log (§7b: no question or answer text, ever) ------------------------------
    def _audit(self, event, **fields):
        """Append one record as one line. If an earlier append failed half-way and left the
        log without a final newline, that fragment is ended first, so it cannot swallow
        this record."""
        record = {"ts": int(self.clock()), "event": event}
        record.update(fields)
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        # The tail check and the whole append happen under one lock (a thread lock, and an
        # exclusive flock on the log for other processes), so no other append can land in
        # the middle of this one.
        with _AUDIT_LOCK, outpath.open_for_write(self.logs / AUDIT_NAME, append=True,
                                                 **_GUARD) as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fd = outpath.open_dir_guarded(self.logs, **_GUARD)
            try:
                r = os.open(AUDIT_NAME, _O_RD, dir_fd=fd)
                try:
                    size = os.fstat(r).st_size
                    if size and os.pread(r, 1, size - 1) != b"\n":
                        line = b"\n" + line
                finally:
                    os.close(r)
                fh.flush()
                _write_all(fh.fileno(), line)
                # Durable before the action it announces: a crash must not keep a deletion
                # whose intent was lost (reconcile() rebuilds only from surviving intents).
                _sync(fh.fileno())
                _sync(fd)
            finally:
                os.close(fd)

    def _audit_events(self):
        """Every complete audit record, read through the guarded logs folder."""
        fd = outpath.open_dir_guarded(self.logs, **_GUARD)
        try:
            try:
                f = os.open(AUDIT_NAME, _O_RD, dir_fd=fd)
            except FileNotFoundError:
                return []
            with os.fdopen(f, encoding="utf-8", errors="replace") as fh:
                out = []
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
                return out
        finally:
            os.close(fd)

    # -- capture -----------------------------------------------------------------------
    def capture(self, question, status_code, payload, reason_code=None, added=(),
                answer_id=None):
        """Keep a failed question and its answer; return the failure id, or None when the
        outcome raised no signal (nothing is written then; an unverified answer is only
        counted). `added` holds signals the outcome cannot raise itself (`user_wrong`, a
        tap). The intent is audited first: if that fails, nothing is stored. The record is
        written under a temporary name and renamed into place, so it is either complete or
        absent. Once it is in place the id is returned even if the completion record is
        lost (reconcile() writes it), so the caller never stores the same answer twice.
        `answer_id`, if given, goes on the intent record, tying it to the trace line."""
        if not all(isinstance(x, str) and x in SIGNALS for x in added):
            raise ValueError("unknown signal")
        _check_answer_id(answer_id)
        signals = sorted(set(signals_for(status_code, payload, reason_code)) | set(added))
        if not signals:
            if (isinstance(payload, dict) and payload.get("status") == "incomplete"
                    and reason_code == UNVERIFIED):
                self._audit("unverified")      # a count: no id, no text, nothing stored
            return None
        now = self.clock()
        fid = "%d-%s" % (int(now), secrets.token_hex(4))
        linked = {"answer_id": answer_id} if answer_id is not None else {}
        self._audit("capturing", id=fid, signals=signals, status_code=status_code,
                    reason_code=reason_code,
                    question_sha256=_digest(question), question_chars=len(question or ""),
                    **linked)
        record = {"id": fid, "ts": int(now), "signals": signals, "status_code": status_code,
                  "reason_code": reason_code,
                  "status": payload.get("status") if isinstance(payload, dict) else None,
                  "question": question,
                  "answer": payload.get("answer", "") if isinstance(payload, dict) else ""}
        data = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        with self._store_lock():
            self._publish(fid, data)
            try:
                self._audit("captured", id=fid)
            except Exception as exc:          # the record is stored: reconcile() records it
                _report("captured", fid, exc)
        return fid

    def add_signal(self, fid, signal, answer_id=None):
        """Record that a stored failure raised one more signal (a tap on an answer already
        captured for another signal): one text-free audit line, no second record and no
        rewrite of the first. True if that failure is stored, False if it is gone."""
        if not (isinstance(fid, str) and _ID.fullmatch(fid)):
            raise ValueError("not a failure id")
        if not (isinstance(signal, str) and signal in SIGNALS):
            raise ValueError("unknown signal")
        _check_answer_id(answer_id)
        linked = {"answer_id": answer_id} if answer_id is not None else {}
        with self._store_lock():
            fd = outpath.open_dir_guarded(self.store, **_GUARD)
            try:
                try:
                    st = os.stat(fid + ".json", dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISREG(st.st_mode):
                    raise OSError("not a regular file")
                self._audit("signal_added", id=fid, signals=[signal], **linked)
                return True
            finally:
                os.close(fd)

    def _publish(self, fid, data):
        """Write the record under a temporary name and rename it into place."""
        fd = outpath.open_dir_guarded(self.store, **_GUARD)
        tmp = TMP_PREFIX + fid
        renamed = False
        try:
            f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | outpath.O_NOFOLLOW, 0o600,
                        dir_fd=fd)
            try:
                _write_all(f, data)
                os.fsync(f)
            finally:
                os.close(f)
            os.rename(tmp, fid + ".json", src_dir_fd=fd, dst_dir_fd=fd)
            renamed = True
            _sync(fd)
        except BaseException as exc:
            if renamed and isinstance(exc, Exception):
                # In place: report the failed sync; raising would hide a stored record.
                _report("publish_sync", fid, exc)
                return
            try:
                os.unlink(tmp, dir_fd=fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)

    def _read_record(self, dir_fd, name):
        """(record or None, file mtime), read without following links."""
        f = os.open(name, _O_RD_RECORD, dir_fd=dir_fd)
        st = os.fstat(f)
        if not stat.S_ISREG(st.st_mode):
            os.close(f)
            raise OSError("not a regular file")
        with os.fdopen(f, encoding="utf-8", errors="replace") as fh:
            mtime = st.st_mtime
            try:
                data = json.loads(fh.read())
            except ValueError:
                return None, mtime
        return (data if isinstance(data, dict) else None), mtime

    def reconcile(self):
        """Complete what the audit log lost, from what is in the store: a valid stored
        record with no 'captured' event gets one; a 'deleting' intent with no 'deleted'
        event gets one if the record is gone (a failed unlink leaves it for the next sweep).
        Returns {"captured": [...], "deleted": [...]}."""
        with self._store_lock():
            return self._reconcile()

    def _reconcile(self):
        events = self._audit_events()
        captured = {e.get("id") for e in events if e.get("event") == "captured"}
        deleting = {e.get("id"): e.get("reason") for e in events if e.get("event") == "deleting"}
        deleted = {e.get("id") for e in events if e.get("event") == "deleted"}
        out = {"captured": [], "deleted": []}
        fd = outpath.open_dir_guarded(self.store, **_GUARD)
        try:
            names = set(os.listdir(fd))
            for name in sorted(names):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                fid = name[:-5]
                if fid in captured:
                    continue
                try:   # each entry on its own: one bad entry must not block the rest
                    record, _ = self._read_record(fd, name)
                    if _valid_record(record, fid):
                        self._audit("captured", id=fid, reconciled=True)
                        out["captured"].append(fid)
                except Exception as exc:
                    try:
                        self._audit("reconcile_error", id=fid, error=type(exc).__name__)
                    except Exception as audit_exc:
                        _report("reconcile_error", fid, audit_exc)
            for fid in sorted(i for i in set(deleting) - deleted if isinstance(i, str)):
                if fid + ".json" not in names:
                    # A lost completion keeps the reason its intent gave (retention or delete()).
                    reason = deleting[fid]
                    if not (isinstance(reason, str) and reason):
                        reason = "retention %d days" % RETENTION_DAYS
                    self._audit("deleted", id=fid, reason=reason, reconciled=True)
                    out["deleted"].append(fid)
        finally:
            os.close(fd)
        return out

    # -- retention ---------------------------------------------------------------------
    def purge(self):
        """Delete every failure older than RETENTION_DAYS and record each deletion. A record
        whose time is missing or not a finite number is aged by its file time. Each record
        is handled on its own: one that cannot be read or deleted is audited as an error and
        the sweep goes on. Leftover temporary files are removed. Listing, reading and
        deleting are all relative to the guarded store folder."""
        with self._store_lock():
            return self._purge()

    def _purge(self):
        now = self.clock()
        cutoff = now - RETENTION_DAYS * 86400
        reason = "retention %d days" % RETENTION_DAYS
        deleted = []
        fd = outpath.open_dir_guarded(self.store, **_GUARD)
        try:
            for name in sorted(os.listdir(fd)):
                if name.startswith(TMP_PREFIX):
                    try:
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        if st.st_mtime < now - TMP_MAX_AGE:
                            os.unlink(name, dir_fd=fd)
                    except OSError:
                        pass
                    continue
                if not name.endswith(".json"):
                    continue
                fid = name[:-5]
                try:
                    record, mtime = self._read_record(fd, name)
                    ts = record.get("ts") if record is not None else None
                    if (ts if _finite_number(ts) else mtime) >= cutoff:
                        continue
                    self._audit("deleting", id=fid, reason=reason)
                    os.unlink(name, dir_fd=fd)
                    _sync(fd)
                    deleted.append(fid)
                    self._audit("deleted", id=fid, reason=reason)   # reconcile() if lost
                except Exception as exc:
                    try:
                        self._audit("purge_error", id=fid, error=type(exc).__name__)
                    except Exception as audit_exc:
                        _report("purge_error", fid, audit_exc)
        finally:
            os.close(fd)
        return deleted

    def delete(self, fid, reason):
        """Delete one stored failure now, on David's request (the planted record of the C4
        end-to-end check, or one he chooses to drop), recorded in the audit log like a
        retention deletion: intent, unlink, completion. `reason` is a key of
        DELETE_REASONS, never free text. Returns True if it was deleted, False if no such failure is stored."""
        _check_delete(fid, reason)
        name = fid + ".json"
        with self._store_lock():
            fd = outpath.open_dir_guarded(self.store, **_GUARD)
            try:
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISREG(st.st_mode):
                    raise OSError("not a regular file")
                self._audit("deleting", id=fid, reason=reason)
                os.unlink(name, dir_fd=fd)
                _sync(fd)
                self._audit("deleted", id=fid, reason=reason)   # reconcile() if lost
                return True
            finally:
                os.close(fd)

    def start_retention(self, interval=RETENTION_INTERVAL):
        """Run reconcile() and purge() now and then every `interval` seconds, in a daemon
        thread, for as long as the service runs. Errors are reported by type only."""
        def loop():
            while True:
                for step in (self.reconcile, self.purge):
                    try:
                        step()
                    except Exception as exc:
                        sys.stderr.write("%s failure retention %s skipped: %s\n"
                                         % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                            step.__name__, type(exc).__name__))
                time.sleep(interval)
        t = threading.Thread(target=loop, name="dbrain-failure-retention", daemon=True)
        t.start()
        return t


if __name__ == "__main__":
    # python3 -m dbrain.failures delete <id> <reason>: run by David in his own Terminal
    # (agents are kept out of the store, D108/D110). Prints the id and the outcome only.
    if len(sys.argv) == 4 and sys.argv[1] == "delete":
        try:
            _check_delete(sys.argv[2], sys.argv[3])     # before the store is even opened
            done = FailureStore().delete(sys.argv[2], sys.argv[3])
        except (ValueError, outpath.OutsideVaultError) as exc:
            raise SystemExit("REFUSED: %s" % exc)
        print("%s %s" % ("deleted" if done else "no such failure", sys.argv[2]))
        raise SystemExit(0 if done else 1)
    raise SystemExit("usage: python3 -m dbrain.failures delete <id> <reason>")
