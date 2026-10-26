"""The enforced boundary. Every read of the vault goes through here.

The invariant is deliberately blunt: **no symlink anywhere between the vault root and
the file**, and the allowlist is checked on the *lexical* path rather than on a resolved
one. Resolution-based authorisation is what made earlier versions breakable - a
symlinked root, or a component swapped between the check and the open, could authorise
its target. So:

  1. the vault-relative path is normalised lexically; any `..` is refused outright;
  2. it must lie inside the active profile's allowlist, compared component by component;
  3. it is walked one component at a time with `openat` + `O_NOFOLLOW`, anchored to the
     vault's own descriptor, so a symlink at any depth fails rather than being followed;
  4. the file is opened read-only, and this module exposes no way to write.

Step 3 also closes the check-then-open race: the descriptor chain *is* the check.
"""
import errno
import os
import unicodedata
from pathlib import Path, PurePosixPath

from . import config

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class ScopeError(Exception):
    """A path outside the allowlist, or reachable only through a symlink."""


def vault_root(vault=None):
    # The vault path itself is operator-supplied configuration, not user input, so it
    # is the one place resolution is allowed.
    return Path(vault or config.VAULT).resolve()


def relative_parts(path, vault=None):
    """Normalise to vault-relative components, refusing `..` and absolute escapes."""
    root = vault_root(vault)
    p = PurePosixPath(str(path))
    if p.is_absolute():
        try:
            p = PurePosixPath(str(Path(str(path)))).relative_to(PurePosixPath(str(root)))
        except ValueError:
            raise ScopeError("absolute path outside the vault: %s" % path)
    parts = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            # Never resolved away: `..` is simply not a legal segment here.
            raise ScopeError("'..' is not allowed in a vault path: %s" % path)
        parts.append(part)
    if not parts:
        raise ScopeError("empty vault path")
    return tuple(parts)


def _fold(parts):
    # The Mac's file system matches names regardless of case and Unicode normalisation, so
    # an exclusion compared exactly would let "90 system/x.md" open "90 System/x.md".
    return tuple(unicodedata.normalize("NFC", p).casefold() for p in parts)


def _excluded(parts, profile):
    """True if `parts` is an excluded folder or file, or lies under one."""
    folded = _fold(parts)
    for e in getattr(profile, "excludes", ()):
        ex = _fold(relative_parts_literal(e))
        if folded[: len(ex)] == ex:  # an empty entry excludes everything: fails closed
            return True
    return False


def _allowed_by_profile(parts, profile):
    if getattr(profile, "exclude_hidden", False) and any(p.startswith(".") for p in parts):
        return False
    if _excluded(parts, profile):
        return False  # an exclusion wins over every root and file
    for f in profile.files:
        if parts == relative_parts_literal(f):
            return True
    for r in profile.roots:
        root = relative_parts_literal(r)
        if len(parts) > len(root) and parts[: len(root)] == root:
            return True
    return False


def relative_parts_literal(rel):
    return tuple(part for part in PurePosixPath(rel).parts if part not in ("", "."))


def check_allowed(path, profile, vault=None):
    """Lexical allowlist check. Returns the normalised parts or raises."""
    parts = relative_parts(path, vault)
    if not _allowed_by_profile(parts, profile):
        raise ScopeError(
            "outside the %r allowlist: %s" % (profile.name, "/".join(parts))
        )
    return parts


def _open_vault_fd(vault=None):
    """Open the vault directory without trusting any ancestor.

    Resolving the vault pathname and then opening it left a window: swap that directory,
    or any ancestor, for a symlink in between and the descriptor points somewhere else,
    after which the anchored child traversal faithfully walks the substituted tree. So
    the vault's own path is walked from "/" with O_NOFOLLOW as well.
    """
    root = vault_root(vault)
    fd = os.open("/", os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            try:
                nxt = os.open(part, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise ScopeError(
                        "refusing a symlinked component %r in the vault path %s"
                        % (part, root)
                    )
                raise ScopeError("cannot open the vault path %s: %s" % (root, exc))
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_anchored(parts, vault=None, directory=False):
    """Walk the components with openat + O_NOFOLLOW. A symlink at any depth fails."""
    fd = _open_vault_fd(vault)
    try:
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            flags = os.O_RDONLY | O_NOFOLLOW
            if not last or directory:
                flags |= O_DIRECTORY
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            except ValueError as exc:
                # e.g. an embedded NUL byte: a refusal, not a crash.
                raise ScopeError("invalid path component %r: %s" % (part, exc))
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise ScopeError(
                        "refusing a symlinked path component %r in %s"
                        % (part, "/".join(parts))
                    )
                if exc.errno == errno.ENOTDIR:
                    raise ScopeError("not a directory: %r in %s" % (part, "/".join(parts)))
                raise ScopeError("cannot open %s: %s" % ("/".join(parts), exc))
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_text(path, profile, vault=None):
    """Read an allowed file. The only read path in the service."""
    parts = check_allowed(path, profile, vault)
    fd = _open_anchored(parts, vault)
    try:
        st = os.fstat(fd)
        import stat as stat_mod

        if not stat_mod.S_ISREG(st.st_mode):
            raise ScopeError("not a regular file: %s" % "/".join(parts))
        if st.st_nlink != 1:
            # Round E finding 3: a hard link is not a symlink, so the lexical allowlist
            # and O_NOFOLLOW both pass an allowlisted name that shares an inode with a
            # forbidden note or an outside file. Refused, loudly, on the open descriptor.
            raise ScopeError("refusing a hard-linked file (%d links): %s"
                             % (st.st_nlink, "/".join(parts)))
        chunks = []
        while True:
            block = os.read(fd, 1 << 16)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def _walk(parts, vault, out, skip_hidden=False):
    """List .md files under a directory, never following or yielding symlinks."""
    root = vault_root(vault)
    base = root.joinpath(*parts)
    try:
        entries = sorted(os.scandir(str(base)), key=lambda e: e.name)
    except (NotADirectoryError, FileNotFoundError, PermissionError):
        return
    for entry in entries:
        if entry.is_symlink():
            continue  # a symlink is never indexed, wherever it points
        if skip_hidden and entry.name.startswith("."):
            continue  # .git, .obsidian, .trash: never descended into
        child = parts + (entry.name,)
        if entry.is_dir(follow_symlinks=False):
            _walk(child, vault, out, skip_hidden)
        elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
            out.append(child)


def iter_allowed_files(profile, vault=None):
    """Every markdown note the profile permits, as vault-relative part tuples."""
    root = vault_root(vault)
    found = []
    for f in profile.files:
        parts = relative_parts_literal(f)
        p = root.joinpath(*parts)
        if p.is_file() and not _has_symlink(parts, root):
            found.append(parts)
    for r in profile.roots:
        parts = relative_parts_literal(r)
        if _has_symlink(parts, root):
            continue  # a symlinked root authorises nothing
        _walk(parts, vault, found, getattr(profile, "exclude_hidden", False))
    seen = set()
    out = []
    for parts in found:
        if not _allowed_by_profile(parts, profile):
            continue  # exclusions and dot-folders, whichever root or file found it
        if parts not in seen:
            seen.add(parts)
            out.append(parts)
    return out


def _has_symlink(parts, root):
    cur = root
    for part in parts:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def as_posix(parts):
    return "/".join(parts)
