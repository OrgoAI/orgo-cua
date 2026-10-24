#!/bin/bash
# orgo-cua: one real Cua agent run on Orgo STAGING: Claude Code, started with
# `cua agent run`, drives an Orgo computer's desktop through cua-spacesd.
# Run it yourself:  bash scripts/agent-test.sh
#
# Reads the Orgo staging key and the Anthropic key from your keychain and hands
# both to scripts/agent_test.py on stdin; they are never printed, saved or put
# on a command line. Everything happens in a new scratch workspace that is
# deleted at the end (computers in other workspaces are counted, never touched).
# The Anthropic key does go inside the scratch computer for the run: it travels
# encrypted, and is gone when the run is removed and the computer deleted.
# It spends a little of the Anthropic key's credit (one short Claude Code task).
# About 5-15 minutes.
# Keychain items (service/account), set to match yours:
#   ORGO_CUA_TEST_ORGO_KEYCHAIN       default ai.orgo.desktop/api-key.staging.orgo.ai
#   ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN  default orgo-cua/anthropic-api-key
# Result: results/agent-run/result.txt (or under ORGO_CUA_RESULTS)
set -u
KIT=$(cd "$(dirname "$0")/.." && pwd)
case "${1:-}" in -h | --help) exec python3 -I "$KIT/scripts/agent_test.py" --help ;; esac
command -v openssl >/dev/null || { echo "openssl is missing."; exit 2; }

OI=${ORGO_CUA_TEST_ORGO_KEYCHAIN:-ai.orgo.desktop/api-key.staging.orgo.ai}
AI=${ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN:-orgo-cua/anthropic-api-key}
K=$(security find-generic-password -s "${OI%%/*}" -a "${OI#*/}" -w 2>/dev/null)
[ -n "$K" ] || { echo "No Orgo staging key found in the keychain ($OI)."; exit 2; }
A=$(security find-generic-password -s "${AI%%/*}" -a "${AI#*/}" -w 2>/dev/null)
[ -n "$A" ] || { echo "No Anthropic key found in the keychain ($AI; set ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN)."; exit 2; }

printf '%s\n%s\n' "$K" "$A" | python3 -I "$KIT/scripts/agent_test.py"
rc=$?
unset K A
echo "Done (exit $rc). Result: ${ORGO_CUA_RESULTS:-$KIT/results}/agent-run/result.txt"
exit "$rc"
