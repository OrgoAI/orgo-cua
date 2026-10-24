#!/bin/bash
# setup-cua-host.sh: make this Orgo Linux computer a Cua host.
#
# Runs as root inside an Orgo computer (sent through POST /computers/{id}/bash).
# It installs a pinned Cua CLI and cua-spacesd (Cua's computer service), sets
# cua-spacesd up on the Orgo desktop (display :99) listening on 127.0.0.1:3211
# only, and keeps it running under supervisord (Orgo computers have no
# systemd). Running it again changes only what is missing or wrong.
#
# The last line is machine-readable:
#   ORGO_CUA_STATUS {"ok": true, "cua": "0.7.0", ...}
# The spacesd token (a root shell on this computer) is never printed: the
# output of `cua host setup`, which contains it, goes to a 0600 log with secrets
# scrubbed out.
#
# Usage:
#   setup-cua-host.sh                     install or repair
#   setup-cua-host.sh --status            report only, change nothing
#   setup-cua-host.sh --agent-deps        install what Cua's agent installs
#                                         need (xz-utils); a separate call, so
#                                         apt never shares setup's 300 s
#   setup-cua-host.sh --pair-file PATH [--name NAME]
#                                         join the user's Cua account with the
#                                         one-time code in PATH (read, then
#                                         deleted at once). The code is never
#                                         taken as an argument, so it stays out
#                                         of the command Orgo receives; Cua
#                                         itself only accepts it as an argument
#                                         (`cua host setup --pair CODE`), so it
#                                         is visible in that one short-lived
#                                         process inside this computer.
#   setup-cua-host.sh --unpair            leave the user's Cua account (Cua's
#                                         own `cua host remove`) and go back to
#                                         the local-only setup
set -euo pipefail
umask 022

CUA_VERSION=0.7.0
SPACESD_VERSION=0.8.0
# sha256 of the release archives, as published by trycua/cua:
#   CLI:     release cua-sdk-v0.7.0, release-artifacts.json
#   spacesd: release cua-spacesd-v0.8.0, <archive>.sha256
CLI_SHA256_X64=014061fa30b553ca58bf6fe9deafcbf3802b15a389979249884d669087c0aac0
CLI_SHA256_ARM64=e2c2b73bb93427cfc4d1d71a66fbc021970f2e701bd5b8b3ba7282ee5fb8216b
SPACESD_SHA256_X86_64=d0b43e96ab6a0fd0ee758195fa4aa1e228848c0472bef4950b028b37b44b168f
SPACESD_SHA256_AARCH64=09c8df972551088ac2b08b52ff3d86345e35a13f250681173af798d3f822b64d
RELEASES=https://github.com/trycua/cua/releases/download

LISTEN=127.0.0.1:3211
DISPLAY_NAME=${ORGO_CUA_DISPLAY:-:99}
PROGRAM=cua-spacesd
PREFIX=/opt/orgo-cua
LOG_DIR=/var/log/orgo-cua
export HOME=/root
unset CUA_HOME
HOST_DIR=$HOME/.cua/host
CUA_BIN=$PREFIX/cua-$CUA_VERSION/cua
SPACESD_BIN=$PREFIX/cua-spacesd-$SPACESD_VERSION/cua-spacesd
# Cua's programs send anonymous usage events unless told not to. This is a
# customer's computer: keep them off.
export DO_NOT_TRACK=1 CUA_TELEMETRY=0

mode_arg=setup
PAIR_FILE=""
PAIR_CODE=""
UNPAIR=""
NAME=${ORGO_CUA_NAME:-}
while [ $# -gt 0 ]; do
    case "$1" in
    --status) mode_arg=status ;;
    --agent-deps) mode_arg=agent-deps ;;
    --unpair) UNPAIR=1 ;;
    --pair-file)
        [ $# -ge 2 ] || { echo "--pair-file needs a path" >&2; exit 2; }
        PAIR_FILE=$2
        shift
        ;;
    --name)
        [ $# -ge 2 ] || { echo "--name needs a value" >&2; exit 2; }
        NAME=$2
        shift
        ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done
if [ -n "$UNPAIR" ] && { [ -n "$PAIR_FILE" ] || [ "$mode_arg" != setup ]; }; then
    echo "--unpair takes no other mode" >&2
    exit 2
fi

changed=()
SUP_CONF=""
SUP_DIR=""
SUP_SUFFIX=".conf"

mark() { changed+=("$1"); }
note() { printf 'orgo-cua: %s\n' "$*"; }
# Drop the pairing code and anything that looks like a token before showing a log.
redact() {
    local line
    while IFS= read -r line; do
        [ -z "$PAIR_CODE" ] || line=${line//"$PAIR_CODE"/<redacted>}
        printf '%s\n' "$line"
    done | sed -E 's/[0-9A-Fa-f]{20,}/<redacted>/g; s/cpc_[^ ]+/<redacted>/g; s/(--token|--pair)[= ]+[^ ]+/\1 <redacted>/g'
}

# Reads the one-time pairing code from PAIR_FILE and deletes the file at once.
read_pair_file() {
    if [ ! -f "$PAIR_FILE" ] || [ -L "$PAIR_FILE" ]; then
        rm -f "$PAIR_FILE"
        fail "pairing code file not found"
    fi
    PAIR_CODE=$(head -c 256 "$PAIR_FILE" | tr -d '\r\n\t ')
    rm -f "$PAIR_FILE"
    rmdir "$(dirname "$PAIR_FILE")" 2>/dev/null || true
    [ -n "$PAIR_CODE" ] || fail "pairing code file was empty"
}

sctl() { supervisorctl -c "$SUP_CONF" "$@"; }

program_state() {
    local s=""
    if [ -n "$SUP_CONF" ]; then
        s=$(sctl status "$PROGRAM" 2>/dev/null | awk -v p="$PROGRAM" '$1 == p { print $2 }') || true
    fi
    printf '%s\n' "${s:-ABSENT}"
}

cua_version() {
    [ -x "$CUA_BIN" ] || return 0
    "$CUA_BIN" --version 2>/dev/null | awk 'NR == 1 { print $NF }' || true
}

# "<mode> <listen>" from the config `cua host setup` wrote ("none -" if unset).
host_mode() {
    python3 -I -c '
import json, sys
try:
    c = json.load(open(sys.argv[1]))
except Exception:
    print("none -")
    sys.exit()
print(c.get("mode") or "none", c.get("listen") or "-")
' "$HOST_DIR/config.json"
}

# `cua spacesd caps` against the local listener. The token goes through the
# environment of that one command, never argv or output.
caps_output() {
    local token
    token=$(cat "$HOST_DIR/env-token" 2>/dev/null) || return 1
    [ -n "$token" ] || return 1
    CUA_ENV_TOKEN=$token timeout 20 "$CUA_BIN" --embedded spacesd caps "http://$LISTEN" 2>/dev/null
}

# emit_status ERROR WAIT_SECONDS: print the ORGO_CUA_STATUS line and exit.
emit_status() {
    local error=$1 wait=$2 cua_v state mode listen caps=skipped sd_v="" missing="" out="" i ok
    if ! command -v python3 >/dev/null 2>&1; then
        echo 'ORGO_CUA_STATUS {"ok": false, "error": "python3 is missing"}'
        exit 1
    fi
    cua_v=$(cua_version)
    state=$(program_state)
    read -r mode listen < <(host_mode) || { mode=none; listen=-; }
    if [ "$mode" = direct ]; then
        caps=fail
        for ((i = 0; i <= wait; i += 2)); do
            if [ "$(program_state)" = RUNNING ] && out=$(caps_output) && [ "${out%% *}" = cua-spacesd ]; then
                caps=ok
                break
            fi
            [ "$i" -lt "$wait" ] && sleep 2
        done
        state=$(program_state)
        if [ "$caps" = ok ]; then
            sd_v=$(printf '%s\n' "$out" | awk 'NR == 1 { print $2 }')
            missing=$(printf '%s\n' "$out" | awk '$1 == "-" { printf "%s ", $2 }')
        fi
    fi
    command -v xz >/dev/null 2>&1 || missing="${missing}xz "
    ok=false
    if [ -z "$error" ] && [ "$cua_v" = "$CUA_VERSION" ] && [ "$state" = RUNNING ]; then
        if { [ "$mode" = direct ] && [ "$caps" = ok ]; } || [ "$mode" = relay ]; then
            ok=true
        fi
    fi
    if [ "$ok" = false ] && [ -z "$error" ]; then
        error="not ready (service $state, mode $mode, caps $caps)"
    fi
    python3 -I -c '
import json, sys
a = sys.argv[1:]
print("ORGO_CUA_STATUS " + json.dumps({
    "ok": a[0] == "true", "cua": a[1], "spacesd": a[2], "mode": a[3],
    "listen": a[4] if a[4] != "-" else "", "service": a[5], "caps": a[6],
    "missing": a[7].split(), "changed": a[8].split(), "error": a[9],
}))
' "$ok" "$cua_v" "${sd_v:-}" "$mode" "$listen" "$state" "$caps" "$missing" "${changed[*]:-}" "$error"
    [ "$ok" = true ]
    exit
}

fail() { emit_status "$1" 0; }

preflight() {
    local missing="" c
    [ "$(id -u)" = 0 ] || fail "must run as root"
    [ "$(uname -s)" = Linux ] || fail "not Linux"
    for c in curl tar sha256sum python3 supervisorctl flock cmp timeout awk sed; do
        command -v "$c" >/dev/null 2>&1 || missing="$missing $c"
    done
    [ -z "$missing" ] || fail "missing tools:$missing"
}

# Cua's agent installs (`cua agent ensure`) unpack Node from a .tar.xz with
# `tar -xJf`, which needs xz. Cua installs unzip when it is missing, not xz,
# and Orgo's image has no xz, so every agent install failed with "node: unpack
# failed". The image's package lists are stale (a plain install gets 404), so
# they are refreshed once when the first try fails. Only agents need xz, so it
# runs as its own call (--agent-deps, at most 105 s of apt); when it cannot be
# installed the status lists "xz" under "missing" and the service is unaffected.
apt_xz() {
    DEBIAN_FRONTEND=noninteractive timeout 30 apt-get install -y -qq --no-install-recommends \
        -o DPkg::Lock::Timeout=20 xz-utils
}
ensure_xz() {
    command -v xz >/dev/null 2>&1 && return 0
    if ! command -v apt-get >/dev/null 2>&1; then
        note "xz is missing and apt-get is not available; Cua's agent installs need xz"
    elif { apt_xz || { timeout 45 apt-get update -qq && apt_xz; }; } >>"$LOG_DIR/apt.log" 2>&1 &&
        command -v xz >/dev/null 2>&1; then
        note "installed xz-utils (Cua's agent installs unpack Node with it)"
        mark xz-utils
    else
        note "could not install xz-utils (see $LOG_DIR/apt.log); Cua's agent installs need xz"
    fi
}

platform() {
    case "$(uname -m)" in
    x86_64 | amd64)
        CLI_PLAT=linux-x64 SD_PLAT=linux-x86_64
        CLI_SHA=$CLI_SHA256_X64 SD_SHA=$SPACESD_SHA256_X86_64
        ;;
    aarch64 | arm64)
        CLI_PLAT=linux-arm64 SD_PLAT=linux-aarch64
        CLI_SHA=$CLI_SHA256_ARM64 SD_SHA=$SPACESD_SHA256_AARCH64
        ;;
    *) fail "unsupported CPU $(uname -m)" ;;
    esac
}

# Finds the running supervisord's config and the folder its [include] reads.
find_supervisor() {
    local found
    found=$(python3 -I -c '
import configparser, os
conf = ""
running = False
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        argv = [a.decode(errors="replace") for a in open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0") if a]
    except OSError:
        continue
    if not any(os.path.basename(a) == "supervisord" for a in argv[:2]):
        continue
    running = True
    for i, a in enumerate(argv):
        if a in ("-c", "--configuration") and i + 1 < len(argv):
            conf = argv[i + 1]
        elif a.startswith("--configuration="):
            conf = a.split("=", 1)[1]
    break
if not running:
    print("ERR supervisord is not running")
    raise SystemExit
if not conf:
    conf = next((p for p in ("/etc/supervisor/supervisord.conf", "/etc/supervisord.conf") if os.path.isfile(p)), "")
if not conf:
    print("ERR supervisord config not found")
    raise SystemExit
cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(";", "#"))
cp.read(conf)
files = cp.get("include", "files", fallback="").split()
pattern = next((f for f in files if "*" in os.path.basename(f)), "")
if not pattern:
    print("ERR supervisord has no [include] folder")
    raise SystemExit
pattern = os.path.join(os.path.dirname(os.path.abspath(conf)), pattern)
base = os.path.basename(pattern)
print("OK", conf, os.path.dirname(pattern), base[base.rindex("*") + 1:] or ".conf")
' || true)
    case "$found" in
    OK\ *) read -r _ SUP_CONF SUP_DIR SUP_SUFFIX <<<"$found" ;;
    *) fail "${found#ERR }" ;;
    esac
}

fetch_verified() { # URL SHA256 DEST
    curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --retry 3 \
        --connect-timeout 20 --max-time 240 -o "$3" "$1" || return 1
    [ "$(sha256sum "$3" | cut -d' ' -f1)" = "$2" ]
}

# install_archive DIR URL SHA256 BINARY: unpack a verified release archive to
# $PREFIX/DIR once; later runs check the installed binary against its record.
install_archive() {
    local dir=$PREFIX/$1 url=$2 sha=$3 bin=$4 tmp
    if [ -x "$dir/$bin" ] && [ "$(cat "$dir/.archive-sha256" 2>/dev/null)" = "$sha" ] &&
        [ "$(sha256sum "$dir/$bin" | cut -d' ' -f1)" = "$(cat "$dir/.binary-sha256" 2>/dev/null)" ]; then
        return 0
    fi
    tmp=$(mktemp -d "$PREFIX/.tmp.XXXXXX")
    if ! fetch_verified "$url" "$sha" "$tmp/archive.tar.gz"; then
        rm -rf "$tmp"
        fail "download or checksum check failed for ${url##*/}"
    fi
    mkdir "$tmp/x"
    tar -xzf "$tmp/archive.tar.gz" -C "$tmp/x" || { rm -rf "$tmp"; fail "could not unpack ${url##*/}"; }
    [ -f "$tmp/x/$bin" ] || { rm -rf "$tmp"; fail "no $bin in ${url##*/}"; }
    chmod 0755 "$tmp/x/$bin"
    printf '%s\n' "$sha" >"$tmp/x/.archive-sha256"
    sha256sum "$tmp/x/$bin" | cut -d' ' -f1 >"$tmp/x/.binary-sha256"
    rm -rf "$dir"
    mv "$tmp/x" "$dir"
    rm -rf "$tmp"
    note "installed $1 (sha256 checked)"
    mark "$1"
}

# install_file DEST MODE < CONTENT: returns 0 when the file changed.
install_file() {
    local dest=$1 mode=$2 tmp
    tmp=$(mktemp "$dest.XXXXXX")
    cat >"$tmp"
    chmod "$mode" "$tmp"
    if [ -f "$dest" ] && cmp -s "$tmp" "$dest"; then
        rm -f "$tmp"
        return 1
    fi
    mv -f "$tmp" "$dest"
}

# The supervisord entry: runs exactly the command `cua host setup` recorded
# in service.json (direct or relay mode), on the Orgo desktop.
write_wrapper() {
    install_file "$PREFIX/bin/run-spacesd" 0755 <<'PY'
"""supervisord entry for cua-spacesd (managed by orgo-cua).

Runs the program, arguments and environment that `cua host setup` wrote to
~/.cua/host/service.json, on the Orgo desktop, with the same log file and
pidfile Cua's own process runner uses, so `cua host status` stays accurate.
"""
import json
import os
import sys
import time

host_dir = os.path.join(os.environ.get("HOME", "/root"), ".cua", "host")
display = os.environ.get("DISPLAY", ":99")
try:
    with open(os.path.join(host_dir, "service.json")) as f:
        spec = json.load(f)
except (OSError, ValueError) as e:
    print(f"run-spacesd: no usable service.json ({e}); run setup-cua-host.sh", file=sys.stderr)
    sys.exit(78)
# After a boot, wait for the X server before starting.
sock = "/tmp/.X11-unix/X" + display.lstrip(":").split(".")[0]
for _ in range(60):
    if os.path.exists(sock):
        break
    time.sleep(0.5)
env = dict(os.environ)
env.update({k: v for k, v in spec.get("env", [])})
env["DISPLAY"] = display
log = spec.get("log_path")
if log:
    os.makedirs(os.path.dirname(log), exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
with open(os.path.join(host_dir, "driver.pid"), "w") as f:
    f.write(str(os.getpid()))
os.chdir(spec.get("working_dir") or "/root")
program = spec["program"]
os.execve(program, [program, *spec.get("args", [])], env)
PY
}

write_program_conf() {
    local py
    py=$(command -v python3)
    install_file "$SUP_DIR/$PROGRAM$SUP_SUFFIX" 0644 <<EOF
; Managed by orgo-cua (setup-cua-host.sh): Cua's computer service on the Orgo desktop.
[program:$PROGRAM]
command=$py -I $PREFIX/bin/run-spacesd
environment=HOME="/root",DISPLAY="$DISPLAY_NAME",DO_NOT_TRACK="1",CUA_TELEMETRY="0"
directory=/root
user=root
autostart=true
autorestart=true
startsecs=3
startretries=20
priority=950
stopsignal=TERM
stopwaitsecs=10
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=$LOG_DIR/supervisor.log
stdout_logfile_maxbytes=1MB
stdout_logfile_backups=2
EOF
}

alive() { # PID: running and not a zombie
    local st
    st=$(awk '{ print $3 }' "/proc/$1/stat" 2>/dev/null) || return 1
    [ -n "$st" ] && [ "$st" != Z ] && [ "$st" != X ]
}

# `cua host setup --runner process` starts its own detached copy; stop it so
# supervisord is the only owner. Only a cua-spacesd process is ever signalled.
stop_detached() {
    local pidfile=$HOST_DIR/driver.pid pid i
    [ -f "$pidfile" ] || return 0
    pid=$(cat "$pidfile" 2>/dev/null || true)
    rm -f "$pidfile"
    case "$pid" in '' | *[!0-9]*) return 0 ;; esac
    alive "$pid" || return 0
    case "$(readlink "/proc/$pid/exe" 2>/dev/null)" in *cua-spacesd*) ;; *) return 0 ;; esac
    kill -TERM "$pid" 2>/dev/null || true
    for ((i = 0; i < 50; i++)); do
        alive "$pid" || return 0
        sleep 0.1
    done
    kill -KILL "$pid" 2>/dev/null || true
}

# run_host_setup ARGS...: `cua host setup ARGS` with supervisord's copy
# stopped; its output (which holds the token) stays in a 0600 log.
run_host_setup() {
    local rc=0 log=$LOG_DIR/host-setup.log
    if [ "$(program_state)" != ABSENT ]; then
        sctl stop "$PROGRAM" >/dev/null 2>&1 || true
    fi
    stop_detached
    (umask 077 && : >"$log")
    DISPLAY=$DISPLAY_NAME "$CUA_BIN" --embedded host setup "$@" \
        --runner process --driver-bin "$SPACESD_BIN" >>"$log" 2>&1 || rc=$?
    stop_detached
    # Keep no secret in the log: Cua prints the direct-mode token, and a
    # pairing error echoes the code. Fail closed: if the scrub fails, keep
    # nothing and show nothing.
    if ! (umask 077 && redact <"$log" >"$log.tmp" && mv -f "$log.tmp" "$log"); then
        rm -f "$log.tmp"
        : >"$log" 2>/dev/null || rm -f "$log"
        note "cua host setup (exit $rc): could not scrub log"
        return "$rc"
    fi
    if [ "$rc" != 0 ]; then
        note "cua host setup failed (exit $rc); last lines, secrets removed:"
        tail -n 5 "$log" | redact
    fi
    return "$rc"
}

# run_host_remove: take this computer out of the user's Cua account with Cua's
# own `cua host remove` (it unregisters from the relay and deletes the host
# setup). supervisord's copy is stopped first, or it would restart the relay
# service; `cua spaces relay-unregister` on another device refuses a computer
# that is still connected.
run_host_remove() {
    local rc=0 log=$LOG_DIR/host-remove.log
    sctl stop "$PROGRAM" >/dev/null 2>&1 || true
    stop_detached
    (umask 077 && : >"$log")
    "$CUA_BIN" --embedded host remove --force >>"$log" 2>&1 || rc=$?
    if [ "$rc" != 0 ]; then
        note "cua host remove failed (exit $rc); last lines, secrets removed:"
        tail -n 5 "$log" | redact
    fi
    return "$rc"
}

main_setup() {
    local mode listen error="" need_restart=0 c
    [ -z "$PAIR_FILE" ] || read_pair_file
    preflight
    exec 9>/run/orgo-cua.lock
    flock -w 120 9 || fail "another setup run is in progress"
    platform
    find_supervisor
    mkdir -p "$PREFIX/bin" "$LOG_DIR"
    chmod 0700 "$LOG_DIR"
    rm -rf "$PREFIX"/.tmp.*
    install_archive "cua-$CUA_VERSION" \
        "$RELEASES/cua-sdk-v$CUA_VERSION/cua-cli-$CUA_VERSION-$CLI_PLAT.tar.gz" "$CLI_SHA" cua
    install_archive "cua-spacesd-$SPACESD_VERSION" \
        "$RELEASES/cua-spacesd-v$SPACESD_VERSION/cua-spacesd-$SD_PLAT.tar.gz" "$SD_SHA" cua-spacesd
    if [ "$(readlink /usr/local/bin/cua 2>/dev/null || true)" != "$CUA_BIN" ]; then
        ln -sfn "$CUA_BIN" /usr/local/bin/cua
        mark cli-link
    fi
    if write_wrapper; then mark wrapper; fi
    # The root user's own `cua` too (Cua prints a telemetry notice otherwise).
    "$CUA_BIN" telemetry off >/dev/null 2>&1 || true

    read -r mode listen < <(host_mode)
    if [ -n "$UNPAIR" ] && [ "$mode" = relay ]; then
        if ! run_host_remove; then
            # Still paired: bring the relay service back before reporting.
            sctl start "$PROGRAM" >/dev/null 2>&1 || true
            fail "could not leave the Cua account (cua host remove failed); the paired service was restarted"
        fi
        note "left the Cua account; back to the local-only setup"
        mark unpaired
        read -r mode listen < <(host_mode)
    fi
    if [ -n "$PAIR_CODE" ]; then
        if run_host_setup --pair "$PAIR_CODE" --name "${NAME:-Orgo $(hostname)}"; then
            mark paired
        else
            error="pairing failed; the computer keeps its previous setup"
        fi
    elif [ "$mode" = relay ]; then
        note "already paired with a Cua account; leaving that setup alone"
    elif [ "$mode" != direct ] || [ "$listen" != "$LISTEN" ] || [ ! -s "$HOST_DIR/env-token" ] ||
        ! cmp -s "$SPACESD_BIN" "$HOST_DIR/bin/cua-spacesd"; then
        run_host_setup --direct "$LISTEN" || fail "cua host setup failed"
        note "cua-spacesd set up on $LISTEN, display $DISPLAY_NAME"
        mark host
    fi

    if write_program_conf; then
        sctl reread >/dev/null 2>&1 || true
        sctl update >/dev/null 2>&1 || true
        mark supervisor
    fi
    for c in "${changed[@]}"; do
        case "$c" in host | paired | wrapper | cua-spacesd-*) need_restart=1 ;; esac
    done
    if [ "$need_restart" = 1 ] || [ "$(program_state)" != RUNNING ]; then
        [ "${#changed[@]}" -gt 0 ] || mark start
        sctl stop "$PROGRAM" >/dev/null 2>&1 || true
        stop_detached
        sctl start "$PROGRAM" >/dev/null 2>&1 || true
    fi
    emit_status "$error" 30
}

main_status() {
    preflight
    find_supervisor
    emit_status "" 10
}

main_agent_deps() {
    preflight
    find_supervisor
    mkdir -p "$LOG_DIR"
    chmod 0700 "$LOG_DIR"
    ensure_xz
    emit_status "" 10
}

if [ "$mode_arg" = status ]; then
    main_status
elif [ "$mode_arg" = agent-deps ]; then
    main_agent_deps
else
    main_setup
fi
