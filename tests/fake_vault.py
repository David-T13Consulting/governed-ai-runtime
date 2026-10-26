"""A synthetic vault, so scope tests can create symlinks and escapes without ever
writing inside the real one."""
import os
import tempfile
from pathlib import Path

from dbrain import config


def build():
    root = Path(tempfile.mkdtemp(prefix="dbrain-vault-"))
    for d in ("20 Areas/Running", "30 Resources/Running", "90 System",
              "10 Projects/AI Second Brain/02_Architecture"):
        (root / d).mkdir(parents=True)

    (root / "20 Areas/Running/athlete-baseline.md").write_text(
        "---\nclassification: internal\n---\n# Baseline\n\nLTHR is 150 bpm, provisional.\n"
    )
    (root / "30 Resources/Running/session-types.md").write_text(
        "---\nclassification: internal\n---\n# Session types\n\nTempo means marathon pace or faster.\n"
    )
    (root / "90 System/secrets.md").write_text(
        "---\nclassification: internal\n---\n# Private\n\nNOT_IN_ANY_ALLOWLIST marker.\n"
    )
    (root / "AGENTS.md").write_text(
        "---\nclassification: internal\n---\n# Rules\n\nVAULT_ROOT_FILE marker.\n"
    )
    outside = root.parent / (root.name + "-outside")
    outside.mkdir(exist_ok=True)
    (outside / "source.md").write_text("OUTSIDE_VAULT marker.\n")

    links = {}
    if hasattr(os, "symlink"):
        # A link inside an allowed root pointing out of the vault.
        links["leak"] = root / "20 Areas/Running/leak.md"
        os.symlink(str(outside / "source.md"), str(links["leak"]))
        # A link inside an allowed root pointing at a forbidden note in the vault.
        links["inward"] = root / "20 Areas/Running/inward.md"
        os.symlink(str(root / "90 System/secrets.md"), str(links["inward"]))
        # An allowlisted ROOT that is itself a symlink into a forbidden area.
        (root / "30 Resources").mkdir(exist_ok=True)
        links["root"] = root / "30 Resources/Local AI"
        os.symlink(str(root / "90 System"), str(links["root"]))
        # An allowlisted FILE that is a symlink to a forbidden note.
        links["file"] = root / "alias-AGENTS.md"
        os.symlink(str(root / "90 System/secrets.md"), str(links["file"]))
        # A symlinked intermediate DIRECTORY inside an allowed root.
        (outside / "nested").mkdir(exist_ok=True)
        (outside / "nested" / "deep.md").write_text("DEEP_OUTSIDE marker.\n")
        links["dir"] = root / "30 Resources/Running/sub"
        os.symlink(str(outside / "nested"), str(links["dir"]))
    return root, outside, links


PROFILE = config.Profile(
    "test",
    roots=("20 Areas/Running", "30 Resources/Running", "30 Resources/Local AI"),
    files=("alias-AGENTS.md",),
)
