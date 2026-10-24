#!/bin/bash
# orgo-cua morning proof (maintainers, Orgo STAGING): the two real end-to-end runs
# before a release, in one command, about 15 minutes:
#   1. a Cua agent (Claude Code via `cua agent run`) drives an Orgo desktop, with an
#      Orgo screenshot about every 2 s and a GIF when ImageMagick is installed;
#   2. an Orgo computer joins your cua.ai account (relay): listed by `cua spaces ls`,
#      one screenshot and one click through Cua from this Mac, then taken out of the
#      account again and deleted.
# Run it yourself:  bash scripts/morning-proof.sh
#
# Keys come from your keychain to the helper on stdin; they are never printed, saved
# or put on a command line. Keychain items (service/account):
#   ORGO_CUA_TEST_ORGO_KEYCHAIN       default ai.orgo.desktop/api-key.staging.orgo.ai
#   ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN  default orgo-cua/anthropic-api-key
# Part 2 needs the `cua` CLI (ORGO_CUA_TEST_CUA, else `cua` on PATH, else
# ~/.local/bin/cua) signed in to your cua.ai account; if it is not, this offers
# `cua auth login` (a browser opens; a free account is enough).
# Result: results/morning/result.txt (or under ORGO_CUA_RESULTS), last line
# RESULT: PASS or FAIL.
set -u
KIT=$(cd "$(dirname "$0")/.." && pwd)
case "${1:-}" in -h | --help) exec python3 -I "$KIT/scripts/morning_proof.py" --help ;; esac
command -v openssl >/dev/null || { echo "openssl is missing."; exit 2; }

CUA=${ORGO_CUA_TEST_CUA:-$(command -v cua || echo "$HOME/.local/bin/cua")}
signed_in() {
    "$CUA" auth status --json 2>/dev/null |
        python3 -I -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("logged_in") is True else 1)' 2>/dev/null
}
RELAY=run
if [ ! -x "$CUA" ]; then
    echo "The cua CLI is not installed, so part 2 (relay) will not run. Cua's own install line (your call):"
    echo "  curl -fsSL https://cua.ai/install.sh | sh -s -- --cli-only"
    RELAY="skip: the cua CLI is not installed"
    CUA=""
elif ! signed_in; then
    echo "Signing in to Cua for part 2 (a browser opens; a free cua.ai account is enough)..."
    DO_NOT_TRACK=1 "$CUA" auth login --no-onboarding || true
    signed_in || RELAY="skip: not signed in to Cua"
fi

OI=${ORGO_CUA_TEST_ORGO_KEYCHAIN:-ai.orgo.desktop/api-key.staging.orgo.ai}
AI=${ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN:-orgo-cua/anthropic-api-key}
K=$(security find-generic-password -s "${OI%%/*}" -a "${OI#*/}" -w 2>/dev/null)
[ -n "$K" ] || { echo "No Orgo staging key found in the keychain ($OI)."; exit 2; }
A=$(security find-generic-password -s "${AI%%/*}" -a "${AI#*/}" -w 2>/dev/null)
[ -n "$A" ] || { echo "No Anthropic key found in the keychain ($AI; set ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN)."; exit 2; }

printf '%s\n%s\n' "$K" "$A" | ORGO_CUA_TEST_CUA="$CUA" ORGO_CUA_TEST_RELAY="$RELAY" python3 -I "$KIT/scripts/morning_proof.py"
rc=$?
unset K A
echo "Done (exit $rc). Result: ${ORGO_CUA_RESULTS:-$KIT/results}/morning/result.txt"
exit "$rc"
