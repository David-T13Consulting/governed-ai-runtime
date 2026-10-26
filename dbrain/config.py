"""Corpus profiles: which parts of the vault a query may read.

A profile is data, not convention. scope.py enforces it; nothing else may widen it.
"""
import os
from pathlib import Path

# The vault the service reads. Defaults to the synthetic sample vault shipped with this
# repository; point DBRAIN_VAULT at a real vault to use one.
SAMPLE_VAULT = Path(__file__).resolve().parent.parent / "examples" / "sample-vault"
VAULT = Path(os.environ.get("DBRAIN_VAULT", SAMPLE_VAULT))

# Traces and results live outside the vault (P2.2 "Done when" 6), in a git-ignored folder
# of this repository (David, Q23, 5 Oct: as lean as possible while keeping the boundary).
# Never in the vault: the vault is the corpus, and every cloud agent reads it.
ARTIFACTS = Path(
    os.environ.get("DBRAIN_ARTIFACTS", Path(__file__).resolve().parent.parent / "artifacts")
)

# The service's §7b logs (data policy v0.7): the per-question trace, the service log and
# the model server's log, with the failure loop's audit log beside them. Outside the vault
# and outside git, on the host (P2.7 step C3b): a git-ignored folder inside a repository
# is still inside git. Measurement evidence stays under ARTIFACTS.
LOGS = Path(
    os.environ.get("DBRAIN_LOGS", Path.home() / "Library" / "Application Support" / "dbrain" / "logs")
)


class Profile:
    """An allowlist. `roots` are directories; `files` are individual notes."""

    def __init__(self, name, roots=(), files=(), sections=None, exclude_hidden=False,
                 skip_unlabelled=False, excludes=(), vault=None):
        self.name = name
        # A fixed vault root for this profile, used when the caller names none. None:
        # the live vault (VAULT). Set only for a frozen evidence corpus (DEVCASE_CORPUS).
        self.vault = vault
        self.roots = tuple(roots)
        self.files = tuple(files)
        # Vault-relative folders or files carved out of the allowlist (P2.8 A1). An entry
        # removes itself and everything under it; it wins over `roots` and `files`, and is
        # compared without regard to case or Unicode form, as the Mac's file system does.
        self.excludes = tuple(excludes)
        # Whole-vault profiles: never read `.git`, `.obsidian` or any other dot-folder.
        self.exclude_hidden = exclude_hidden
        # Whole-vault profiles: a note with no `classification`, another label, or
        # frontmatter outside the allowlisted language is skipped and recorded instead of
        # failing every query (Q46); it never reaches the model. A recognisable `client`
        # label still stops every query (index.ProhibitedNote). Narrow profiles keep the
        # strict default: fail loudly.
        self.skip_unlabelled = skip_unlabelled
        # Optional {file: (start_prefix, end_prefix)} to bound what is indexed inside a
        # file. The 1 Oct retrieval baseline indexed only section 0 of the roadmap, so
        # reproducing its cases means indexing the same span, not the whole note.
        self.sections = dict(sections or {})

    def __repr__(self):
        return "Profile(%r, %d roots, %d files)" % (self.name, len(self.roots), len(self.files))


# The allowlist P2.2 specifies, as Cowork corrected it on 2 Oct ("Done when" 2): exactly
# the corpus the 10 dev cases were written against - three named files, with the roadmap
# bounded to section 0, and no roots. The first version of the brief listed the Running
# and Local AI folders and 02_Architecture as roots; no dev case covers them, and a root
# over 02_Architecture let evaluation notes - including the 5 Oct results note itself -
# into the corpus they measure (round C finding 1). Widening the corpus (M5) is a
# separate, versioned change with its own cases.
#
# Run reports record a content hash of the indexed spans, so a note that changes between
# runs shows up as a different corpus rather than silently.
SCOPED_FILES = (
    "AGENTS.md",
    "10 Projects/AI Second Brain/02_Architecture/data-policy.md",
    "10 Projects/AI Second Brain/04_Roadmap.md",
)
SCOPED_SECTIONS = {
    # Section 0 is the live amendment; sections 1+ are superseded work packages.
    # The 1 Oct prototype bounded the roadmap the same way.
    "10 Projects/AI Second Brain/04_Roadmap.md": ("## 0.", "## 1."),
}

# The dev-case corpus is frozen: every model is compared on the same evidence. In this
# public repository it is the synthetic sample vault; the original measurements ran on a
# frozen copy of the owner's real notes, which is not published. Never edit these files
# without updating the pinned hashes in tests/test_response.py: a changed corpus is a new
# baseline.
DEVCASE_CORPUS = SAMPLE_VAULT

DEFAULT = Profile("default", files=SCOPED_FILES, sections=SCOPED_SECTIONS, vault=DEVCASE_CORPUS)

# Kept as a name because run reports since 2 Oct record it. Same allowlist as DEFAULT
# now that the brief's allowlist *is* the dev-case corpus.
DEVCASES = Profile("devcases", files=SCOPED_FILES, sections=SCOPED_SECTIONS, vault=DEVCASE_CORPUS)

# The system's own working notes, which the phone must not search (P2.8 A1, D133, 14 Oct).
# Since 5 Oct they quote test questions almost word for word, so keyword retrieval ranked
# them above the life notes the questions are about. An exclude list rather than an
# allowlist, so a new life folder is found without a config change. `00 Inbox` stays in.
SYSTEM_NOTES = (
    "90 System",                                   # briefs, handoff, for-david, reports
    "10 Projects/AI Second Brain",                 # this project: PROGRESS, DECISIONS, specs
    "40 Archive/agent-orchestration-proposal.md",  # the agents' own design record
    "AGENTS.md",                                   # the agents' rulebook
    "CLAUDE.md",
    "index.md",                                    # the vault catalogue
    "log.md",                                      # the agents' operations log
)

# MVP-phone (David, Q34, 5 Oct): the whole vault, read-only, for questions from the
# iPhone. Root "" is the vault itself. Dot-folders are excluded; the reserved acceptance
# cases live outside the vault, so they are unreachable by construction. Not used by the
# dev-case measurements, which stay on the three-file corpus. The name is kept because
# traces record it; since P2.8 the profile is the whole vault minus SYSTEM_NOTES.
MVP_WHOLE_VAULT = Profile("mvp-whole-vault", roots=("",), exclude_hidden=True,
                          skip_unlabelled=True, excludes=SYSTEM_NOTES)

PROFILES = {p.name: p for p in (DEFAULT, DEVCASES, MVP_WHOLE_VAULT)}
