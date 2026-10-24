#!/bin/bash
# agent-test.sh: one Cua agent run on this Orgo computer (sent by scripts/agent_test.py).
#
# Runs as root inside an Orgo computer that setup-cua-host.sh already made a
# Cua host (cua-spacesd on 127.0.0.1:3211). Orgo records every /bash command
# and its output, so no secret appears in either: the Anthropic key arrives
# encrypted to a one-time RSA key made here, is decrypted straight into the
# environment of one `cua agent run`, and the private key is deleted at once.
#
#   agent-test.sh prepare               add this desktop to Cua as a direct
#                                       sandbox, install Claude Code, make the
#                                       one-time key pair (prints the public key)
#   agent-test.sh start CIPHER NONCE [MODEL]
#                                       decrypt the key and start the run
#   agent-test.sh poll RUN_ID           the run's status
#   agent-test.sh finish RUN_ID         the run's events, result and proof file;
#                                       then stop and delete the run (Cua's
#                                       record of it holds the key)
#
# The last line is machine-readable: ORGO_CUA_AGENT {json}
set -euo pipefail
umask 077

export HOME=/root DO_NOT_TRACK=1 CUA_TELEMETRY=0
CUA_BIN=${ORGO_CUA_BIN:-/usr/local/bin/cua}
WORK=${ORGO_CUA_AGENT_DIR:-/root/.orgo-cua-agent}
TOKEN_FILE=${ORGO_CUA_TOKEN_FILE:-/root/.cua/host/env-token}
PROOF=${ORGO_CUA_PROOF:-/tmp/cua-agent-proof.txt}
SB=orgo-desk
LISTEN=127.0.0.1:3211

# cua SECONDS ARGS...: one cua command; the spacesd token reaches it through
# its environment only.
cua() {
    local t=$1
    shift
    CUA_ENV_TOKEN=$(cat "$TOKEN_FILE") timeout "$t" "$CUA_BIN" --embedded "$@"
}

scrub() { sed -E 's/sk-ant-[A-Za-z0-9_-]+/<redacted>/g; s/[0-9A-Fa-f]{20,}/<redacted>/g'; }

# report ERROR [KEY VALUE ...] < `cua --json agent status` output (or nothing):
# print the status line and exit (1 when ERROR is set).
report() {
    scrub | python3 -I -c '
import json, sys
v = {}
for line in sys.stdin:
    try:
        x = json.loads(line)
    except ValueError:
        continue
    if isinstance(x, dict):
        v = x
s = v.get("status") if isinstance(v.get("status"), dict) else {}
r = v.get("result") if isinstance(v.get("result"), dict) else {}
a = sys.argv[1:]
out = {"ok": not a[0], "error": a[0]}
if v:
    out.update(status=s.get("status", ""), phase=s.get("phase", ""), turn=r.get("turn"),
               stop_reason=r.get("stop_reason"), run_error=r.get("error"),
               tool_calls=r.get("tool_calls"), text=(r.get("text") or "")[:300])
out.update(zip(a[1::2], a[2::2]))
print("ORGO_CUA_AGENT " + json.dumps(out))
' "$@"
    [ -z "$1" ]
    exit
}

prepare() {
    local out rc=0 pub
    [ -s "$TOKEN_FILE" ] || report "not a Cua host yet (run orgo-cua up first)" </dev/null
    # Cua unpacks Node with xz; setup-cua-host.sh installs it (ensure_xz).
    command -v xz >/dev/null 2>&1 || report "xz is missing, so Cua cannot unpack Node (run orgo-cua up again)" </dev/null
    # Forget any earlier record (the machine keeps running), then add this
    # desktop the way a Cua user adds any machine that runs cua-spacesd.
    # Timeouts add up to under Orgo's 300 s limit for one bash call.
    cua 20 sb rm "$SB" >/dev/null 2>&1 || true
    out=$(cua 40 sb create --on "direct:$LISTEN" --name "$SB" 2>&1) || rc=$?
    printf '%s\n' "$out" | tail -n 5 | scrub
    [ "$rc" = 0 ] || report "cua sb create failed (exit $rc)" </dev/null
    cua 15 agent harnesses 2>&1 | scrub || true
    # Install Claude Code before any key is here (a later rerun reuses it).
    out=$(cua 210 agent ensure "$SB" claude-code 2>&1) || rc=$?
    printf '%s\n' "$out" | tail -n 10 | scrub
    [ "$rc" = 0 ] || report "installing claude-code failed or timed out (exit $rc)" </dev/null
    mkdir -p "$WORK"
    rm -f "$WORK/agent.key"
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$WORK/agent.key" 2>/dev/null ||
        report "could not make the one-time key pair" </dev/null
    pub=$(openssl pkey -in "$WORK/agent.key" -pubout)
    report "" pub "$pub" </dev/null
}

start() {
    local cipher=$1 nonce=$2 model=${3:-} prompt out rc=0 run_id
    [ -f "$WORK/agent.key" ] || report "no one-time key here (run prepare first)" </dev/null
    ANTHROPIC_API_KEY=$(printf '%s' "$cipher" | base64 -d 2>/dev/null |
        openssl pkeyutl -decrypt -inkey "$WORK/agent.key" -pkeyopt rsa_padding_mode:oaep 2>/dev/null) ||
        ANTHROPIC_API_KEY=""
    rm -f "$WORK/agent.key"
    [ -n "$ANTHROPIC_API_KEY" ] || report "could not decrypt the key" </dev/null
    export ANTHROPIC_API_KEY
    rm -f "$PROOF"
    # The proof line names the program that ran it: typed into a desktop
    # terminal, it says xfce4-terminal; run by the agent's own shell, it does not.
    # Orgo's desktop (Xtigervnc, no /dev/uinput) cannot take Cua's background
    # clicks and keys in GTK apps, so the agent is told to use foreground
    # (README, "Background input").
    # shellcheck disable=SC2016 # $(...) and $PPID are for the desktop terminal
    prompt=$(printf 'This is a test of desktop control on a Linux computer. Use only the desktop tools of the cua MCP server (screenshot, mouse, keyboard, opening apps). Do not use your own shell, terminal or file tools.
Use delivery_mode "foreground" for every click and key press: this desktop cannot take background input.
1. Take a screenshot.
2. Open a terminal window on the desktop (xfce4-terminal).
3. Click into it, type exactly this line, then press Enter:
echo %s $(ps -o comm= -p $PPID) > %s
4. Take a screenshot to check that the command ran, then reply DONE.' "$nonce" "$PROOF")
    out=$(cua 120 --json agent run "$SB" claude-code "$prompt" --env-from-host ANTHROPIC_API_KEY \
        --exit-when-idle --label orgo-agent-test ${model:+--model "$model"} 2>&1) || rc=$?
    out=${out//"$ANTHROPIC_API_KEY"/<redacted>}
    unset ANTHROPIC_API_KEY
    printf '%s\n' "$out" | tail -n 10 | scrub
    run_id=$(printf '%s\n' "$out" | python3 -I -c '
import json, sys
for line in sys.stdin:
    try:
        v = json.loads(line)
    except ValueError:
        continue
    if isinstance(v, dict) and v.get("run_id"):
        print(v["run_id"])
' | tail -n 1)
    [ "$rc" = 0 ] && [ -n "$run_id" ] || report "cua agent run failed (exit $rc)" </dev/null
    report "" run_id "$run_id" </dev/null
}

poll() {
    local out
    out=$(cua 60 --json agent status "$SB" "$1" 2>&1) || {
        printf '%s\n' "$out" | tail -n 5 | scrub
        report "cua agent status failed" </dev/null
    }
    printf '%s\n' "$out" | report ""
}

finish() {
    local run=$1 proof status removed=no
    echo "--- agent events"
    cua 60 agent logs "$SB" "$run" 2>&1 | tail -n 200 | scrub || true
    echo "--- end of agent events"
    status=$(cua 60 --json agent status "$SB" "$run" 2>/dev/null) || status=""
    proof=$(head -n 1 "$PROOF" 2>/dev/null | head -c 200 | scrub) || proof=""
    cua 60 agent stop "$SB" "$run" >/dev/null 2>&1 || true
    if cua 60 agent rm "$SB" "$run" >/dev/null 2>&1; then removed=yes; fi
    rm -rf "$WORK"
    printf '%s\n' "$status" | report "" proof "$proof" removed "$removed"
}

case "${1:-}" in
prepare) prepare ;;
start) [ $# -ge 3 ] || report "start needs CIPHER NONCE" </dev/null; start "$2" "$3" "${4:-}" ;;
poll) [ $# -ge 2 ] || report "poll needs RUN_ID" </dev/null; poll "$2" ;;
finish) [ $# -ge 2 ] || report "finish needs RUN_ID" </dev/null; finish "$2" ;;
*) report "usage: agent-test.sh prepare|start|poll|finish" </dev/null ;;
esac
