#!/bin/sh
# MVP-phone: run the local model and the /ask service, restarting either if it exits,
# and keep the Air awake while on power (caffeinate -s) for as long as this runs.
# Started by "Ask Brain.command" (a Login Item) or by hand. Ctrl-C stops both.
set -u
# Every log is a file per start (appended, never truncated, if a restart reuses the same
# second), opened by `dbrain.outpath exec-logged`: outside the
# vault and outside git, checked on the same descriptor chain it is opened through, never
# through a symlink (data policy §7b, P2.7 step C3b). No shell redirect writes a log.
HERE=$(cd "$(dirname "$0")/.." && pwd)
APP="$HOME/Library/Application Support/dbrain"
SERVER="$APP/llama/llama-b11146/llama-server"
# Default model: Gemma 4 26B-A4B QAT Q4_0 (google, rev d1c082be), picked by T9c and adopted by
# David (Q152, 21 Oct): 7 of 8 dev cases against C2's 5, same typical latency, ~15 GiB while
# running. To go back to C2 (Gemma 4 E4B QAT Q4_0, Q84 (a)), swap the two pairs and restart.
MODEL="$APP/models/gemma-4-26B_q4_0-it.gguf"
MODEL_PIN=3eca3b8f6d7baf218a7dd6bba5fb59a56ee25fe2d567b6f5f589b4f697eca51d
# MODEL="$APP/models/gemma-4-E4B_q4_0-it.gguf"
# MODEL_PIN=676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee
# Logs outside the vault and outside git (data policy §7b, P2.7 step C3b): config.LOGS,
# ~/Library/Application Support/dbrain/logs unless DBRAIN_LOGS says otherwise. The service
# gets the checked absolute path back, so a relative DBRAIN_LOGS cannot resolve elsewhere.
LOGS=$(PYTHONPATH="$HERE" python3 -m dbrain.outpath logdir \
    "$(PYTHONPATH="$HERE" python3 -c 'from dbrain import config; print(config.LOGS)')") || exit 1
logged() { PYTHONPATH="$HERE" python3 -m dbrain.outpath exec-logged "$@"; }
# P2.7 failure capture (D119, David 9 Oct): "on" keeps a failed question and its answer in
# the store for 90 days (data policy §7a). To switch it off, set "off" and restart this
# launcher; stored failures are still deleted on schedule.
CAPTURE=on
[ -x "$SERVER" ] && [ -f "$MODEL" ] || { echo "run mvp/prepare-model.sh first" >&2; exit 1; }
[ "$(shasum -a 256 "$MODEL" | cut -d' ' -f1)" = "$MODEL_PIN" ] || { echo "PIN MISMATCH $MODEL" >&2; exit 1; }

# Model server: loopback only, offline, the settings T9 measured (P2.2 run B plus --reasoning off).
( while :; do
    logged "$LOGS/llama-server-$(date +%Y%m%dT%H%M%S).log" \
    "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 18087 --offline --no-webui -np 1 \
        -c 4096 -t 4 -ngl 99 --no-cache-prompt --reasoning off
    rc=$?
    logged "$LOGS/restart-llama-$(date +%Y%m%dT%H%M%S).log" \
        printf '%s\n' "$(date '+%F %T') llama-server exited $rc; restarting in 10 s"
    sleep 10
  done ) &
LLAMA_LOOP=$!

# /ask service: binds to the Tailscale address only (refuses to start without one).
( cd "$HERE" && while :; do
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u GOOGLE_API_KEY -u GEMINI_API_KEY \
        -u MISTRAL_API_KEY -u COHERE_API_KEY -u HF_TOKEN \
        DBRAIN_LOGS="$LOGS" DBRAIN_FAILURE_CAPTURE="$CAPTURE" \
        DBRAIN_MODEL="$(basename "$MODEL" .gguf)" \
        PYTHONPATH="$HERE" python3 -m dbrain.outpath exec-logged \
        "$LOGS/ask-$(date +%Y%m%dT%H%M%S).log" python3 -m dbrain.serve
    rc=$?
    logged "$LOGS/restart-ask-$(date +%Y%m%dT%H%M%S).log" \
        printf '%s\n' "$(date '+%F %T') ask service exited $rc; restarting in 30 s"
    sleep 30
  done ) &
ASK_LOOP=$!

# Keep-awake on power, held for exactly as long as this script runs.
/usr/bin/caffeinate -s -w $$ &
trap 'pkill -P $LLAMA_LOOP; pkill -P $ASK_LOOP; kill $LLAMA_LOOP $ASK_LOOP 2>/dev/null; exit 0' INT TERM HUP
echo "dbrain MVP running (logs: $LOGS). Keep this window open; Ctrl-C to stop."
wait
