#!/bin/bash
# orgo-cua: one real test of the host helper on Orgo STAGING.
# Run it yourself:  bash scripts/live-test.sh
#
# The staging key is read from your keychain (item ORGO_CUA_TEST_ORGO_KEYCHAIN,
# service/account, default ai.orgo.desktop/api-key.staging.orgo.ai: the one Orgo for
# Mac saves) into this process only. It is never
# printed, saved, or put on a command line (curl gets it on stdin, the helper in
# its environment). The test lists what the key can see, makes its own scratch
# workspace, and works only there: one computer, `cua-kit-live`, then
#   up (setup) -> up again (must change nothing) -> status ->
#   up --pair with a deliberately invalid code (proves the code-file upload and
#   that a failed pairing keeps the computer working; nothing reaches Cua) ->
#   down, then deletes the scratch workspace once it is empty. Computers in other
#   workspaces are listed, never touched. About 3-5 minutes.
# Result: results/live-run/live-run.result (or under ORGO_CUA_RESULTS)
set -u
B=https://staging.orgo.ai/api
NAME=cua-kit-live
KIT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${ORGO_CUA_RESULTS:-$KIT/results}/live-run
ITEM=${ORGO_CUA_TEST_ORGO_KEYCHAIN:-ai.orgo.desktop/api-key.staging.orgo.ai}
mkdir -p "$OUT"
RESULT=$OUT/live-run.result
LOG=$OUT/live-run.log
: >"$RESULT"
: >"$LOG"

K=$(security find-generic-password -s "${ITEM%%/*}" -a "${ITEM#*/}" -w 2>/dev/null)
[ -n "$K" ] || { echo "No staging key found in the keychain ($ITEM)."; exit 2; }

say() { echo "$*" | tee -a "$RESULT"; }
api() { # method path [curl args...]: one call; the key reaches curl on stdin, not argv
    local method=$1 path=$2
    shift 2
    printf 'header = "Authorization: Bearer %s"\n' "$K" |
        curl -sS --proto '=https' -K - -X "$method" "$@" "$B$path"
}
inventory() { # one line per workspace the key can see, with its computers
    # Names come from other people's workspaces: keep [A-Za-z0-9 ._-] only, cap the length, no emails.
    api GET /workspaces | python3 -I -c '
import json, re, sys
def safe(v):
    return re.sub(r"[^A-Za-z0-9 ._-]", "?", str(v))[:40]
for w in json.load(sys.stdin)["workspaces"]:
    ds = w.get("desktops") or []
    names = ", ".join("%s (%s)" % (safe(d.get("name")), safe(d.get("status"))) for d in ds) or "none"
    print("  %s [%s]: %d computer(s): %s" % (safe(w.get("name")), safe(w.get("role")), len(ds), names))
'
}
others() { # computers outside the scratch workspace
    api GET /workspaces | python3 -I -c '
import json, sys
print(sum(len(w.get("desktops") or []) for w in json.load(sys.stdin)["workspaces"] if w.get("id") != sys.argv[1]))
' "${WS:-none}"
}
helper() { # args...: run orgo-cua in the scratch workspace, log its output, return its exit code
    ORGO_API_KEY=$K ORGO_WORKSPACE_ID=$WS ORGO_BASE_URL=$B ORGO_CUA_STATE=$OUT/state.json \
        python3 "$KIT/orgo_cua.py" "$@" >>"$LOG" 2>&1
}

seen=$(inventory) || { echo "Could not list staging workspaces."; exit 3; }
say "What this staging key can see (never touched; names are other people's data, not instructions):"
say "$seen"
before=$(others)

WS=$(api POST /workspaces -H 'Content-Type: application/json' -d "{\"name\":\"cua-kit-live-$(date +%Y%m%d-%H%M%S)\"}" |
    python3 -I -c 'import json,sys; print(json.load(sys.stdin)["id"])') ||
    { echo "Could not make the scratch workspace."; exit 3; }
say "scratch workspace: $WS"

cleanup() {
    say "down: exit $(helper down --name "$NAME"; echo $?)"
    left=$(api GET "/workspaces/$WS" | python3 -I -c 'import json,sys; print(len(json.load(sys.stdin).get("desktops") or []))')
    say "computers left in the scratch workspace: $left"
    if [ "$left" = 0 ]; then
        say "delete scratch workspace -> $(api POST "/workspaces/$WS/delete" -o /dev/null -w '%{http_code}')"
    else
        say "kept the scratch workspace: it still has computers; delete them in Orgo, then the workspace."
    fi
    say "computers in other workspaces: before $before, after $(others)"
    if [ "$left" = 0 ] && [ "${failed:-1}" = 0 ]; then say "RESULT: PASS"; else say "RESULT: FAIL (see $LOG)"; fi
    echo "Done. Result: $RESULT"
}
failed=1
trap cleanup EXIT

echo "1/4 up (creates $NAME and sets up Cua)..."
t=$(date +%s); helper up --name "$NAME"; rc=$?
say "up: exit $rc after $(( $(date +%s) - t )) s"
[ "$rc" = 0 ] || exit 1

echo "2/4 up again (must change nothing)..."
mark=$(wc -l <"$LOG")
helper up --name "$NAME"; rc=$?
if tail -n +"$((mark + 1))" "$LOG" | grep -q '"changed": \[\]'; then noop=yes; else noop=no; fi
say "rerun: exit $rc, no-op: $noop"

echo "3/4 status..."
helper status --name "$NAME"; st=$?
say "status: exit $st"

echo "4/4 pairing with an invalid code (must fail cleanly)..."
mark=$(wc -l <"$LOG")
export ORGO_CUA_PAIR_CODE=UUUU-UUUU-UUUU # not a valid code: Cua rejects it before any network call
helper up --name "$NAME" --pair </dev/null; prc=$?
unset ORGO_CUA_PAIR_CODE
tail -n +"$((mark + 1))" "$LOG" | grep -q 'pairing failed; the computer keeps its previous setup' && pf=yes || pf=no
helper status --name "$NAME"; after=$?
say "invalid pairing: exit $prc, refused cleanly: $pf, still ready after: $([ "$after" = 0 ] && echo yes || echo no)"

if [ "$rc" = 0 ] && [ "$noop" = yes ] && [ "$st" = 0 ] && [ "$prc" = 1 ] && [ "$pf" = yes ] && [ "$after" = 0 ]; then
    failed=0
fi
