#!/bin/sh
# Done when 7: no credential-shaped string in this repository.
#
# Scope, stated rather than implied: this scans the files of this repo, which is what
# "anywhere the agent can read" means for the code being delivered. It is not a
# whole-disk scan and does not claim to be one.
#
# Self-tests first against a planted credential: a scanner that cannot fail proves
# nothing. Exit 2 means the scanner is broken, 1 means it found something, 0 is clean.
set -u

PATTERNS='sk-[A-Za-z0-9_-]{16,}
ghp_[A-Za-z0-9]{20,}
AKIA[0-9A-Z]{16}
-----BEGIN [A-Z ]*PRIVATE KEY-----
[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}
(password|passwd|secret|token|api[_-]?key)[[:space:]]*[:=][[:space:]]*.?[A-Za-z0-9_-]{12,}'

# Options precede the pattern and path: after `--` they would be treated as operands.
#
# Round D finding 5: the loop used to read the patterns from a pipe, so it ran in a
# subshell and its rc=2 was lost; a scan of a missing target printed errors and exited 0.
# Now the loop runs in this shell, a missing target is an error, and every caller checks
# the return code, not only the output.
GREP=grep
# Test double for the self-test: a grep that fails the way a real error does (status 2).
_failing_grep() { return 2; }

scan() {
  target=$1
  [ -e "$target" ] || { echo "SECRET SCAN ERROR: no such target $target" >&2; return 2; }
  rc=0
  old_ifs=$IFS
  IFS='
'
  set -f   # the patterns contain glob characters
  for p in $PATTERNS; do
    [ -n "$p" ] || continue
    "$GREP" -rInE --exclude-dir=.git --exclude-dir=__pycache__ \
      --exclude=secret_scan.sh -e "$p" "$target"
    status=$?
    # grep: 0 = match, 1 = no match, >1 = a real error we must not swallow.
    if [ "$status" -gt 1 ]; then
      echo "SECRET SCAN ERROR: grep failed with status $status on $target" >&2
      rc=2
    fi
  done
  set +f
  IFS=$old_ifs
  return $rc
}

tmp=$(mktemp -d) || { echo "cannot create temp dir" >&2; exit 2; }
key="sk""-abcdefghijklmnopqrstuvwxyz123456"
printf 'OPENAI_API_KEY = "%s"\n' "$key" > "$tmp/planted.py"
found=$(scan "$tmp" 2>/dev/null); rc=$?
if [ "$rc" -eq 2 ] || [ -z "$found" ]; then
  echo "SECRET SCAN BROKEN: it did not catch a planted credential. Fix the patterns." >&2
  rm -rf "$tmp"
  exit 2
fi
# The error path must work too: a missing target has to be an error, not "clean".
scan "$tmp/missing" >/dev/null 2>&1; rc=$?
if [ "$rc" -ne 2 ]; then
  rm -rf "$tmp"
  echo "SECRET SCAN BROKEN: a missing target returned $rc, not 2." >&2
  exit 2
fi
# And a grep error inside the pattern loop, against a target that exists (round E finding
# 7: the original bug lost exactly this status in a subshell).
GREP=_failing_grep
scan "$tmp" >/dev/null 2>&1; rc=$?
GREP=grep
rm -rf "$tmp"
if [ "$rc" -ne 2 ]; then
  echo "SECRET SCAN BROKEN: a grep error inside the scan returned $rc, not 2." >&2
  exit 2
fi

hits=$(scan .); rc=$?
if [ "$rc" -eq 2 ]; then
  echo "SECRET SCAN ERROR: the scan itself failed; the result is not clean." >&2
  exit 2
fi
if [ -n "$hits" ]; then
  echo "SECRET SCAN FAILED:"
  echo "$hits"
  exit 1
fi
echo "secret scan clean (self-test passed): no credential-shaped strings in $(pwd)"
