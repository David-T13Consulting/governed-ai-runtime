"""Guard for everything this service writes.

**Threat model, stated (round E):** this guard stops a misconfigured, symlinked,
hard-linked or `..`-laden destination from landing a write in the vault, and an ancestor
swapped for a symlink between check and open. It does **not** stop a concurrent process
running as David that re-links or moves the destination *while* it is being written: such a
process can already write the vault directly, so no in-process check could add a boundary
against it. A link created after the open is still detected when the file is closed.

`scope.py` guards reads. Writes need their own guard, because the destinations are
configurable (`DBRAIN_ARTIFACTS`, `--out`) and a symlinked or `..`-laden path could
otherwise aim a trace or a report into the vault and truncate a note.

Two separate jobs:
  * `assert_outside_vault` answers "where would a write to this path actually land?"
    using `realpath`, which resolves symlinks *and* normalises `..` even when the tail
    does not exist yet. Appending an unresolved tail to a resolved ancestor was the
    earlier bug: `/x/new/../vault/note.md` slipped through.
  * `open_for_write` then walks that real path from "/" one component at a time with
    `openat` + `O_NOFOLLOW`, creating missing directories as it goes, and opens the file
    relative to its parent's descriptor. Opening the absolute path was the round C bug:
    `O_NOFOLLOW` covers only the last component, so an *ancestor* swapped for a symlink
    after the check redirected the write. The descriptor chain is the check, as in
    `scope.py`, so there is no window after it.
"""
import errno
import os
import stat
from pathlib import Path

from . import config

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class OutsideVaultError(Exception):
    """A write destination resolved inside the vault, or through a symlink. Refused."""


def _real(path):
    # realpath resolves symlinks and collapses `..` without requiring the path to exist.
    return Path(os.path.realpath(str(path)))


def _vault_identity(vault=None):
    """(st_dev, st_ino) of the vault root: the filesystem's own answer to "is this the
    vault?". Paths are not enough on a case-insensitive volume, where `dbrain2` and
    `Dbrain2` are one directory (round F finding 1)."""
    try:
        st = os.stat(str(_real(Path(vault or config.VAULT))))
    except OSError:
        return None  # no vault at that path: nothing to alias
    return (st.st_dev, st.st_ino)


def assert_outside_vault(path, vault=None):
    """Return the real destination if writing there cannot touch the vault, else raise."""
    root = _real(Path(vault or config.VAULT))
    target = _real(path)
    vid = _vault_identity(vault)
    for p in [target] + list(target.parents):
        try:
            st = os.stat(str(p))
        except OSError:
            continue  # not created yet
        if (st.st_dev, st.st_ino) == vid:
            raise OutsideVaultError(
                "refusing to write inside the vault: %s is the vault under another name" % p
            )
    if target == root or _inside(target, root):
        raise OutsideVaultError(
            "refusing to write inside the vault: %s would land at %s" % (path, target)
        )
    # Every existing ancestor must also sit outside, so a parent symlink cannot be
    # followed into the vault on the way down.
    for ancestor in list(target.parents):
        if ancestor == root or _inside(ancestor, root):
            raise OutsideVaultError(
                "refusing a write whose parent %s is inside the vault" % ancestor
            )
        if ancestor.exists():
            break
    return target


def assert_outside_git(path):
    """Refuse a destination inside a git working tree (data policy §7a and §7b: the
    failure store and its logs live outside git). Walks the real path upwards looking for
    a `.git` entry; a git-ignored folder inside a repository is still inside git."""
    target = _real(path)
    for p in [target] + list(target.parents):
        if os.path.lexists(str(p / ".git")):
            raise OutsideVaultError(
                "refusing to write inside a git working tree: %s is under %s" % (path, p))
    return target


def _has_git(fd):
    """A `.git` entry (directory, or file as in a worktree) in the directory open at fd."""
    try:
        os.stat(".git", dir_fd=fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _open_dir_anchored(directory, vault=None, outside_git=False, private=False):
    """Walk an absolute, already-real directory path with O_NOFOLLOW, creating as needed.
    Every directory opened on the way is compared with the vault by identity, so no
    alias or case variant can walk the write into it. With `outside_git`, every directory
    on the same descriptor chain is also checked for a `.git` entry, so the check and the
    write cannot be separated by a path change. With `private`, the last directory must be
    owned by this user with no group or other access (P2.7 failure store and its logs)."""
    vid = _vault_identity(vault)
    fd = os.open("/", os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        if outside_git and _has_git(fd):
            raise OutsideVaultError("refusing to write inside a git working tree (/)")
        for part in directory.parts[1:]:
            flags = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass  # created concurrently; the open below still refuses a symlink
                nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) == vid:
                raise OutsideVaultError("refusing to write under the vault (reached as %s)" % directory)
            if outside_git and _has_git(fd):
                raise OutsideVaultError("refusing to write inside a git working tree (%s)" % directory)
        if private:
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
                raise OutsideVaultError("refusing %s: not private to this user (mode %o)"
                                        % (directory, stat.S_IMODE(st.st_mode)))
        return fd
    except OutsideVaultError:
        os.close(fd)
        raise
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            raise OutsideVaultError(
                "refusing to write under %s: a component is a symlink or not a "
                "directory (changed after the check?)" % directory
            )
        raise OutsideVaultError("cannot open %s for writing: %s" % (directory, exc))
    except BaseException:
        os.close(fd)
        raise


def _strict_real(path, vault=None, strict=False):
    """assert_outside_vault, and with `strict` the path must already be its own real path:
    a caller that resolved it earlier refuses a symlink swapped in since, instead of
    following it to wherever it now points (P2.7 store and logs)."""
    target = assert_outside_vault(path, vault)
    if strict and target != Path(os.path.abspath(str(path))):
        raise OutsideVaultError("refusing %s: it now resolves elsewhere (a symlink?)" % path)
    return target


def open_dir_guarded(path, vault=None, outside_git=False, private=False, strict=False):
    """A descriptor for a guarded directory (created if missing), for callers that list,
    read and delete inside it relative to that descriptor (P2.7 retention)."""
    target = _strict_real(path, vault, strict)
    return _open_dir_anchored(target, vault, outside_git=outside_git, private=private)


def open_for_write(path, vault=None, append=False, outside_git=False, private=False,
                   strict=False):
    """Open a guarded destination, refusing to write through a symlink at any depth.

    Missing parent directories are created inside the same anchored walk, so callers
    must not `mkdir` the destination themselves: a pre-created path would be followed
    through whatever it had become by then.
    """
    target = _strict_real(path, vault, strict)
    dir_fd = _open_dir_anchored(target.parent, vault, outside_git=outside_git, private=private)
    # No O_TRUNC here: truncating at open would change the file before the link check
    # below could refuse it. Truncation happens on the checked descriptor instead.
    flags = os.O_WRONLY | os.O_CREAT | O_NOFOLLOW
    if append:
        flags |= os.O_APPEND
    try:
        fd = os.open(target.name, flags, 0o600, dir_fd=dir_fd)
    except OSError as exc:
        raise OutsideVaultError(
            "refusing to write to %s: %s (a symlink there would be followed)"
            % (target, exc)
        )
    finally:
        os.close(dir_fd)
    try:
        _refuse_shared_inode(fd, target)
        if private:
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
                raise OutsideVaultError("refusing %s: not private to this user (mode %o)"
                                        % (target, stat.S_IMODE(st.st_mode)))
        if not append:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return _CheckedFile(os.fdopen(fd, "a" if append else "w", encoding="utf-8"), target)


class _CheckedFile:
    """A text file that re-checks its link count when closed, so a hard link created
    after the open is reported instead of passing silently (round E finding 2)."""

    def __init__(self, fh, target):
        self._fh = fh
        self._target = target

    def write(self, text):
        return self._fh.write(text)

    def flush(self):
        return self._fh.flush()

    def fileno(self):
        return self._fh.fileno()

    def close(self):
        try:
            self._fh.flush()
            _refuse_shared_inode(self._fh.fileno(), self._target)
        finally:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _refuse_shared_inode(fd, target):
    """Round D finding 2: a hard link is not a symlink, so realpath and O_NOFOLLOW both
    pass it, and writing through it changes the vault note it shares an inode with. A
    destination must be a regular file with exactly one link. Checked on the opened
    descriptor, so the file that is checked is the file that is written."""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise OutsideVaultError("refusing to write to %s: not a regular file" % target)
    if st.st_nlink != 1:
        raise OutsideVaultError(
            "refusing to write to %s: it has %d hard links, and another one may be a "
            "vault note" % (target, st.st_nlink)
        )


def make_dir(path, vault=None, fresh=True):
    """Create a guarded output directory and return its real path.

    For callers that are not Python (measure.sh): they get a directory that passed the
    same check and anchored walk as every other write, then work inside it by its real
    path. `fresh` refuses an existing directory, so evidence is never mixed between runs.
    """
    target = assert_outside_vault(path, vault)
    if not fresh:
        os.close(_open_dir_anchored(target, vault))
        return target
    # Round E finding 1: checking existence and then creating left a window in which a
    # directory (or a link) made by someone else was accepted as ours. mkdir is atomic:
    # it fails on anything already at that name.
    parent = _open_dir_anchored(target.parent, vault)
    try:
        os.mkdir(target.name, 0o700, dir_fd=parent)
    except FileExistsError:
        raise OutsideVaultError("refusing: %s already exists" % target)
    finally:
        os.close(parent)
    os.close(_open_dir_anchored(target, vault))
    return target


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def exec_logged(log_path, argv):
    """Run argv with stdout and stderr on a log file opened through the anchored walk with
    the outside-git check (P2.7 step C3b, data policy §7b). The check and the open are one
    descriptor chain, so the file cannot be redirected into the vault or a repository
    between them. Replaces this process (exec); returns only by raising."""
    if not argv:
        raise OutsideVaultError("exec-logged: no command to run")
    with open_for_write(log_path, append=True, outside_git=True) as fh:   # never truncates
        fd = os.dup(fh.fileno())
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    # python3 -m dbrain.outpath mkdir <path>: used by measure.sh before its first write.
    # python3 -m dbrain.outpath logdir <path>: create and check a log folder (outside the
    #     vault and outside git); prints its real path. Used by mvp/run-all.sh.
    # python3 -m dbrain.outpath exec-logged <log file> <command> [args...]: used by
    #     mvp/run-all.sh for every log it writes.
    import sys

    try:
        if len(sys.argv) == 3 and sys.argv[1] == "mkdir":
            print(make_dir(sys.argv[2]))
        elif len(sys.argv) == 3 and sys.argv[1] == "logdir":
            os.close(open_dir_guarded(sys.argv[2], outside_git=True))
            print(assert_outside_git(sys.argv[2]))
        elif len(sys.argv) >= 4 and sys.argv[1] == "exec-logged":
            exec_logged(sys.argv[2], sys.argv[3:])
        else:
            raise SystemExit("usage: python3 -m dbrain.outpath mkdir <path> | logdir <path> | "
                             "exec-logged <log file> <command> [args...]")
    except OutsideVaultError as exc:
        raise SystemExit("REFUSED: %s" % exc)
