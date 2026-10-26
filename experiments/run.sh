#!/bin/sh
# Latency sweep wrapper: pinned binaries, guarded output directory, no network.
#   ./experiments/run.sh <label>
set -eu
set -C
LABEL=${1:?usage: run.sh <label>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
ARCHIVE=/private/tmp/dbrain2-llama-b11146.tar.gz
# The model left /private/tmp on 5 Oct (Q42); same path and override as measure.sh.
MODEL=${DBRAIN_MODEL:-"$HOME/Library/Application Support/dbrain/models/Qwen3-4B-Q4_K_M.gguf"}
export DBRAIN_MODEL="$MODEL"
ARCHIVE_PIN=1ad3f9eff80edb9dbef4259ad564d1720612ef7eea48fa4afed0e54f5f3d5711
MODEL_PIN=7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5

OUT=$(PYTHONPATH="$HERE" python3 -m dbrain.outpath mkdir "$HERE/artifacts/$LABEL") || exit 1
cd -P "$OUT"
[ "$(pwd -P)" = "$OUT" ] || { echo "refusing: $OUT moved" >&2; exit 1; }

# Written once, with `>` under noclobber: `>>` would follow a link planted at the name.
# A function, not a split string: the model path contains a space.
check_pin() {
    got=$(shasum -a 256 "$1" | cut -d' ' -f1)
    [ "$got" = "$2" ] || { echo "PIN MISMATCH $1" >&2; exit 1; }
    echo "$1 sha256 $got (matches pin)"
}
{ check_pin "$ARCHIVE" "$ARCHIVE_PIN"; check_pin "$MODEL" "$MODEL_PIN"; } > ./pins.txt

BIN=$(mktemp -d /private/tmp/dbrain-llama.XXXXXX)
tar -xzf "$ARCHIVE" -C "$BIN"
SERVER=$(find "$BIN" -name llama-server -type f | head -1)
"$SERVER" --version > ./server-version.txt 2>&1 || true

cat > ./sandbox-nonet.sb <<EOF
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:18087"))
(allow network-outbound (remote unix-socket))
EOF
{
    sandbox-exec -f ./sandbox-nonet.sb curl -s -o /dev/null -m 10 https://huggingface.co \
        && echo "off-box REACHED (bad)" || echo "off-box exit $? (could not connect, as intended)"
} > ./network-check.txt 2>&1
grep -q "REACHED" ./network-check.txt && { echo "network not blocked" >&2; exit 1; }

env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GOOGLE_API_KEY -u GEMINI_API_KEY \
    -u MISTRAL_API_KEY -u COHERE_API_KEY -u HF_TOKEN \
    sandbox-exec -f ./sandbox-nonet.sb \
    python3 "$HERE/experiments/latency_sweep.py" "$SERVER" "$OUT" > ./sweep.stdout 2>&1
echo "done: $OUT"
