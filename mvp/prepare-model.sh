#!/bin/sh
# MVP-phone: put the pinned runtime and model where a long-running service can use them.
# /private/tmp is cleared at reboot, and ~/Documents is privacy-protected for background
# processes, so the stable home is ~/Library/Application Support/dbrain/. Hash-checked
# against the 1 Oct pins before anything is copied or used (M6 budget: same files).
set -eu
DEST="$HOME/Library/Application Support/dbrain"
SRC_ARCHIVE=${1:-/private/tmp/dbrain2-llama-b11146.tar.gz}
SRC_MODEL=${2:-/private/tmp/dbrain2-Qwen3-4B-Q4_K_M.gguf}
ARCHIVE_PIN=1ad3f9eff80edb9dbef4259ad564d1720612ef7eea48fa4afed0e54f5f3d5711
MODEL_PIN=7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5
HERE=$(cd "$(dirname "$0")/.." && pwd)
# Every write below lands under $DEST: refuse a $DEST reached through a symlink, or one
# that resolves inside the vault (outpath guard, the same check as every other write),
# before creating anything, and again once the folders exist.
guard() { PYTHONPATH="$HERE" python3 -c '
import os, sys
from dbrain import outpath
home = os.path.abspath(os.environ["HOME"])
for p in sys.argv[1:]:
    q = os.path.abspath(p)
    while q.startswith(home + os.sep):  # every component below $HOME, the file itself too
        if os.path.islink(q):
            sys.exit("refusing: %s is a symlink" % q)
        q = os.path.dirname(q)
    outpath.assert_outside_vault(p)
' "$DEST" "$DEST/models" "$DEST/llama" "$DEST/models/Qwen3-4B-Q4_K_M.gguf" || exit 1; }
guard
mkdir -p "$DEST/models" "$DEST/llama"
guard
pin() { [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ] || { echo "PIN MISMATCH $1" >&2; exit 1; }; }
# The old baseline (Qwen3-4B) is optional since Q84 (a): checked if present, copied if its
# source is given, otherwise skipped. The service itself runs the default model below.
if [ -f "$DEST/models/Qwen3-4B-Q4_K_M.gguf" ]; then
    pin "$DEST/models/Qwen3-4B-Q4_K_M.gguf" "$MODEL_PIN"
elif [ -f "$SRC_MODEL" ]; then
    pin "$SRC_MODEL" "$MODEL_PIN"
    cp "$SRC_MODEL" "$DEST/models/Qwen3-4B-Q4_K_M.gguf"
    pin "$DEST/models/Qwen3-4B-Q4_K_M.gguf" "$MODEL_PIN"
else
    echo "baseline Qwen3-4B not installed (optional)"
fi
if [ ! -x "$DEST/llama/llama-b11146/llama-server" ]; then
    pin "$SRC_ARCHIVE" "$ARCHIVE_PIN"
    tar -xzf "$SRC_ARCHIVE" -C "$DEST/llama"
fi
# The default model from Q84 (a), 8 Oct, to Q152, 21 Oct: Gemma 4 E4B QAT Q4_0, fetched under the T9 manifest
# (models/MANIFEST.md). This script never downloads it; it only checks the pin.
[ -f "$DEST/models/gemma-4-E4B_q4_0-it.gguf" ] || { echo "missing $DEST/models/gemma-4-E4B_q4_0-it.gguf (see models/MANIFEST.md)" >&2; exit 1; }
pin "$DEST/models/gemma-4-E4B_q4_0-it.gguf" 676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee
# The default model since Q152, 21 Oct: Gemma 4 26B-A4B QAT Q4_0, fetched under the T9c manifest
# (artifacts/t9c-2026-10-21/). E4B above stays as the one-restart fallback.
[ -f "$DEST/models/gemma-4-26B_q4_0-it.gguf" ] || { echo "missing $DEST/models/gemma-4-26B_q4_0-it.gguf (see the T9c manifest)" >&2; exit 1; }
pin "$DEST/models/gemma-4-26B_q4_0-it.gguf" 3eca3b8f6d7baf218a7dd6bba5fb59a56ee25fe2d567b6f5f589b4f697eca51d
echo "ready: $DEST"
