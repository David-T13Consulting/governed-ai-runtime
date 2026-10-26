"""The one writer (PL1 steps 6-7): the only code in this service allowed to write inside
the vault. Step 6 uses one operation, `create_inbox_note`: one new file in `00 Inbox/`,
never an edit, never an overwrite, never anywhere else.

How it lands, so a half-written note is never visible and nothing is followed:
  * the vault's real path is opened from "/" one directory at a time with `O_NOFOLLOW`,
    then `00 Inbox` the same way (it must already exist: the writer creates no folder);
  * the bytes go to a hidden temporary file in that folder, opened `O_CREAT | O_EXCL |
    O_NOFOLLOW` relative to the folder's descriptor, written in full and synced;
  * the note appears under its final name by `link()`, which fails if the name exists, so
    a note is never replaced; on a clash the next suffix (-2 ... -9) is tried;
  * the temporary name is removed whatever happens, and the result is checked: a regular
    file, one link, exactly the bytes written.

`outpath.py`'s vault exceptions are not used: this module is the vault write, and it has
none of its own beyond `00 Inbox`. The index is rebuilt on every question
(`answer.ask` calls `index.build`), so a saved note is searchable from the next /ask.
"""
import os
import secrets
import stat
from pathlib import Path

from . import capture

INBOX = "00 Inbox"
MAX_NOTE = 8192
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC
HEADER = b"---\nclassification: internal\n"


class WriteRefused(Exception):
    """The note was not placed. Nothing was left in the vault."""


class TempLeft(WriteRefused):
    """The note was not placed, but the writer's own hidden temporary file could not be
    removed: it is still in `00 Inbox/` and must be deleted there by hand."""


class NoteChanged(Exception):
    """The note was placed, but what followed could not be confirmed: the temporary name
    could not be removed, the check found it changed or replaced (a concurrent process),
    or the folder could not be synced. It is left where it is, never deleted, and the
    approval counts as used: saying it again would make a second copy."""


def _open_dir_chain(path):
    """A descriptor for `path` (absolute, already real), reached from "/" one component
    at a time without following any link. The caller closes it."""
    fd = os.open("/", _DIR)
    try:
        for part in Path(path).parts[1:]:
            nxt = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _candidates(name):
    yield name
    stem = name[:-3]
    for n in range(2, 10):
        yield "%s-%d.md" % (stem, n)


def create_inbox_note(vault, name, data):
    """Write `data` as a new note `00 Inbox/<name>` (or `<name>-n`) in `vault`. Returns
    the vault-relative path written. Raises WriteRefused when nothing was placed (TempLeft
    if the writer's own temporary file could not be removed), NoteChanged for any failure
    after the note was placed (reported, never deleted by the service). Only a file this
    call created is ever removed."""
    if not isinstance(name, str) or not capture.NAME.fullmatch(name):
        raise WriteRefused("bad note name")
    if not isinstance(data, bytes) or not data.startswith(HEADER) or len(data) > MAX_NOTE:
        raise WriteRefused("bad note content")
    if vault is None:
        raise WriteRefused("no vault")
    root = os.path.realpath(str(vault))
    try:
        vfd = _open_dir_chain(root)
    except OSError:
        raise WriteRefused("vault not reachable")
    try:
        try:
            ifd = os.open(INBOX, _DIR, dir_fd=vfd)
        except OSError:
            raise WriteRefused("no Inbox folder (or it is a link)")
    finally:
        os.close(vfd)
    tmp = ".capture-%s.tmp" % secrets.token_hex(8)
    created = False
    made = None
    try:
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_CLOEXEC,
                         0o600, dir_fd=ifd)
        except OSError:
            raise WriteRefused("could not create the note")
        created = True
        try:
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fchmod(fd, 0o644)
            os.fsync(fd)
        except OSError:
            raise WriteRefused("could not write the note")
        finally:
            os.close(fd)
        for cand in _candidates(name):
            try:
                os.link(tmp, cand, src_dir_fd=ifd, dst_dir_fd=ifd, follow_symlinks=False)
            except FileExistsError:
                continue
            except OSError:
                raise WriteRefused("could not place the note")
            made = cand
            break
        if made is None:
            raise WriteRefused("too many notes with that name this minute")
    except BaseException:
        # Not placed. Remove only the temporary file this call created; a name that was
        # already there when O_EXCL refused it is never touched.
        try:
            if created and not _remove(ifd, tmp):
                raise TempLeft(tmp)
        finally:
            os.close(ifd)
        raise
    # Placed. From here on nothing is removed but the temporary name, and every failure
    # says the note exists.
    try:
        if not _remove(ifd, tmp):
            raise NoteChanged(made)
        _verify(ifd, made, data)
        os.fsync(ifd)
    except NoteChanged:
        raise
    except Exception:
        raise NoteChanged(made)
    finally:
        os.close(ifd)
    return "%s/%s" % (INBOX, made)


def _remove(ifd, tmp):
    """Remove this call's temporary file. True if it is gone."""
    try:
        os.unlink(tmp, dir_fd=ifd)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _verify(ifd, name, data):
    st = os.stat(name, dir_fd=ifd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size != len(data):
        raise NoteChanged(name)
    fd = os.open(name, os.O_RDONLY | O_NOFOLLOW | O_CLOEXEC, dir_fd=ifd)
    try:
        got = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            got += chunk
    finally:
        os.close(fd)
    if got != data:
        raise NoteChanged(name)
