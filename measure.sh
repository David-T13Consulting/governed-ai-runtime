#!/bin/sh
# Measured dev-case run (P2.2 "Done when" 5), with its evidence kept together.
#
# Round C finding 5: the 5 Oct run's RSS samples, launch command and network check
# lived only in a session scratchpad at first. This script writes all of them, plus
# both pass reports, into one evidence directory outside the vault.
#
#   ./measure.sh <label>          e.g. ./measure.sh 2026-10-05-b
#
# Needs: the llama.cpp archive under /private/tmp (volatile) and the model in
# ~/Library/Application Support/dbrain/models/ (mvp/prepare-model.sh puts it there; the
# /private/tmp copy was deleted on 5 Oct, Q42). DBRAIN_MODEL overrides the model path. Both
# hashes are checked against the 1 Oct pins before anything runs.
set -eu
set -C   # noclobber: a `>` never overwrites a file that is already there

LABEL=${1:?usage: measure.sh <label>}
HERE=$(cd "$(dirname "$0")" && pwd)
ART=${DBRAIN_ARTIFACTS:-$HERE/artifacts}
ARCHIVE=/private/tmp/dbrain2-llama-b11146.tar.gz
MODEL=${DBRAIN_MODEL:-"$HOME/Library/Application Support/dbrain/models/Qwen3-4B-Q4_K_M.gguf"}
ARCHIVE_PIN=1ad3f9eff80edb9dbef4259ad564d1720612ef7eea48fa4afed0e54f5f3d5711
MODEL_PIN=7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5
PORT=18087

# Round D finding 1: this script used to `mkdir` and redirect into $EV itself, so with
# DBRAIN_ARTIFACTS pointing into the vault it wrote there before Python refused anything.
# Now the write guard creates the directory (refusing the vault, symlinks at any depth and
# an existing directory) and the script works *inside* it: after `cd -P`, every write is
# relative to a working directory held by inode, so swapping an ancestor afterwards
# cannot redirect it. Nothing is written before this point.
EV=$(PYTHONPATH="$HERE" python3 -m dbrain.outpath mkdir "$ART/$LABEL-evidence") || exit 1
cd -P "$EV" || exit 1
[ "$(pwd -P)" = "$EV" ] || { echo "refusing: $EV moved after the check" >&2; exit 1; }

# Round E finding 1: `>>` ignores noclobber and follows a link planted at the name, so
# every evidence file is written exactly once, with `>` under noclobber (O_EXCL).
check_pin() {
    got=$(shasum -a 256 "$1" | cut -d' ' -f1)
    [ "$got" = "$2" ] || { echo "PIN MISMATCH $1: $got" >&2; exit 1; }
    echo "$1 sha256 $got (matches pin)"
}
{ check_pin "$ARCHIVE" "$ARCHIVE_PIN"; check_pin "$MODEL" "$MODEL_PIN"; } > ./pins.txt

# Next to the pinned archive, not under $TMPDIR, which could be set to anywhere.
BIN=$(mktemp -d /private/tmp/dbrain-llama.XXXXXX)
tar -xzf "$ARCHIVE" -C "$BIN"
SERVER=$(find "$BIN" -name llama-server -type f | head -1)

cat > "./sandbox-nonet.sb" <<EOF
(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:$PORT"))
(allow network-outbound (remote unix-socket))
EOF

# An argument list, not a string: the model path contains a space (Q42).
set -- "$SERVER" -m "$MODEL" --host 127.0.0.1 --port "$PORT" --offline --no-webui -np 1 -c 4096 -t 4 -ngl 99 --no-cache-prompt
printf '%s\n' "$*" > "./launch.txt"
"$@" > "./llama-server.log" 2>&1 &
SPID=$!
trap 'kill $SPID 2>/dev/null || true' EXIT

i=0
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do
    i=$((i + 1)); [ $i -gt 120 ] && { echo "server did not come up" >&2; exit 1; }
    sleep 1
done

# Network check under the same profile the runner uses: off-box must fail, local must work.
{
    echo "off-box: curl https://huggingface.co under the profile"
    sandbox-exec -f "./sandbox-nonet.sb" curl -s -o /dev/null -m 10 https://huggingface.co \
        && echo "  REACHED (bad)" || echo "  exit $? (could not connect, as intended)"
    echo "local: /health under the profile"
    sandbox-exec -f "./sandbox-nonet.sb" curl -s -m 10 "http://127.0.0.1:$PORT/health"
    echo
} > "./network-check.txt" 2>&1
grep -q "REACHED" "./network-check.txt" && { echo "network not blocked; stopping" >&2; exit 1; }

# Server RSS in KiB, every 0.25 s, for the whole of both passes.
( while kill -0 $SPID 2>/dev/null; do ps -o rss= -p $SPID; sleep 0.25; done ) > "./server-rss.log" &
RPID=$!

for pass in 1 2; do
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GOOGLE_API_KEY -u GEMINI_API_KEY \
        -u MISTRAL_API_KEY -u COHERE_API_KEY -u HF_TOKEN \
        sandbox-exec -f "./sandbox-nonet.sb" \
        python3 "$HERE/run_dev_cases.py" --profile devcases --out "$EV/dev-cases-pass$pass.json" \
        > "./pass$pass.stdout" 2>&1
done

kill $RPID 2>/dev/null || true
echo "peak server RSS KiB: $(sort -n "./server-rss.log" | tail -1)" > ./peak-rss.txt
echo "evidence: $EV"
