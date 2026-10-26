#!/bin/sh
# MVP-phone, David runs this once: create the /ask bearer token in the login Keychain.
#
# The token is never written to a file and never printed. It goes into the Keychain item
# "dbrain-mvp-ask" and onto the clipboard for 120 s, so it can be pasted into the Shortcut
# (Universal Clipboard reaches the iPhone). Caveat: `security` takes the value as an
# argument, so it is visible to local processes for the instant the command runs.
#   ./mvp/make-token.sh            create (refuses to overwrite)
#   ./mvp/make-token.sh --rotate   replace the existing token; update the Shortcut after
set -eu
SERVICE=dbrain-mvp-ask
if /usr/bin/security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
    [ "${1:-}" = "--rotate" ] || { echo "a token already exists; use --rotate to replace it" >&2; exit 1; }
    /usr/bin/security delete-generic-password -s "$SERVICE" >/dev/null
fi
TOKEN=$(/usr/bin/openssl rand -base64 36 | tr -d '\n/+=' | cut -c1-40)
/usr/bin/security add-generic-password -s "$SERVICE" -a "$USER" -w "$TOKEN" -T /usr/bin/security
printf '%s' "$TOKEN" | /usr/bin/pbcopy
TOKEN=
( sleep 120; printf '' | /usr/bin/pbcopy ) >/dev/null 2>&1 &
echo "Token stored in the Keychain (item \"$SERVICE\") and copied to the clipboard for 120 s."
echo "Paste it into the Shortcut's Authorization header now: Bearer <paste>"
