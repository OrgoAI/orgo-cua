"""Shell-level tests of guest/setup-cua-host.sh: the real run_host_setup and
redact functions, run by bash with a fake `cua` that prints secrets."""

import json
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "guest" / "setup-cua-host.sh"
PAIR = "K7QM-4XPB-2RHD"
BASH = shutil.which("bash") or "/bin/bash"


def function_source(name: str) -> str:
    """The text of one shell function from the guest script."""
    text = SCRIPT.read_text()
    one_line = re.search(rf"^{name}\(\) \{{.*\}}$", text, re.M)
    if one_line:
        return one_line.group(0)
    block = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
    if not block:
        raise AssertionError(f"{name} not found in {SCRIPT}")
    return block.group(0)


class RunHostSetupLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.token = secrets.token_hex(24)  # the shape of a spacesd env token

    def run_setup(self, cua_exit: int, break_mv: bool = False) -> tuple[str, Path]:
        fake_cua = self.tmp / "fake-cua"
        fake_cua.write_text(
            "#!/bin/sh\n"
            f"echo 'On your laptop, add this computer:'\n"
            f"echo '  cua spaces add http://127.0.0.1:3211 --host --token {self.token}'\n"
            f"echo 'cua: invalid argument: \"{PAIR}\" is not a pairing code'\n"
            f"exit {cua_exit}\n"
        )
        fake_cua.chmod(fake_cua.stat().st_mode | stat.S_IXUSR)
        harness = "\n".join([
            "set -euo pipefail",
            function_source("note"),
            function_source("redact"),
            function_source("run_host_setup"),
            "program_state() { echo ABSENT; }",
            "sctl() { :; }",
            "stop_detached() { :; }",
            "mv() { return 1; }" if break_mv else "",
            f"LOG_DIR={self.tmp} DISPLAY_NAME=:99 SPACESD_BIN=/nonexistent PAIR_CODE={PAIR} CUA_BIN={fake_cua}",
            f"run_host_setup --pair {PAIR} && echo rc=0 || echo rc=$?",
        ])
        res = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout + res.stderr, self.tmp / "host-setup.log"

    def assert_no_secret(self, text: str):
        self.assertNotIn(self.token, text)
        self.assertNotIn(PAIR, text)

    def test_failed_setup_shows_scrubbed_lines_only(self):
        out, log = self.run_setup(cua_exit=3)
        self.assertIn("rc=3", out)
        self.assertIn("last lines, secrets removed", out)
        self.assertIn("is not a pairing code", out)
        self.assert_no_secret(out)
        self.assert_no_secret(log.read_text())
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)

    def test_failed_scrub_fails_closed(self):
        out, log = self.run_setup(cua_exit=3, break_mv=True)
        self.assertIn("rc=3", out)
        self.assertIn("could not scrub log", out)
        self.assertNotIn("is not a pairing code", out)  # no log line is shown at all
        self.assert_no_secret(out)
        self.assertTrue(not log.exists() or log.read_text() == "")
        self.assertFalse((self.tmp / "host-setup.log.tmp").exists())

    def test_successful_setup_prints_nothing_and_keeps_a_scrubbed_log(self):
        out, log = self.run_setup(cua_exit=0)
        self.assertIn("rc=0", out)
        self.assertNotIn("cua spaces add", out)
        self.assert_no_secret(log.read_text())
        self.assertIn("--token <redacted>", log.read_text())


# A stand-in apt-get. MODE: ok (install works), stale (install gets 404 until
# `update` has run, as on Orgo's image), down (everything fails), noxz (install
# "works" but no xz appears). A working install puts an xz on PATH.
FAKE_APT = """#!/bin/sh
echo "${DEBIAN_FRONTEND:--} $*" >> "$CALLS"
if [ "$1" = update ]; then
    [ "$MODE" = down ] && exit 100
    : > "$STATE/updated"
    exit 0
fi
case "$MODE" in
stale) [ -f "$STATE/updated" ] || { echo "E: Failed to fetch xz-utils.deb  404  Not Found"; exit 100; } ;;
down) exit 100 ;;
noxz) exit 0 ;;
esac
printf '#!/bin/sh\\nexit 0\\n' > "$BIN/xz"
/bin/chmod +x "$BIN/xz"
"""


class FakeBin:
    """A PATH of stand-ins and links to the few real tools a harness needs."""

    def make_bin(self, *real: str) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in real:
            (self.bin / name).symlink_to(shutil.which(name))
        # `timeout SECONDS CMD...` records SECONDS, then runs CMD.
        self.tool("timeout", '#!/bin/sh\necho "$1" >> "$TIMEOUTS"\nshift\nexec "$@"\n')

    def tool(self, name: str, body: str) -> None:
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def env(self, mode: str | None) -> dict:
        if mode is not None:
            self.tool("apt-get", FAKE_APT)
        return {"PATH": str(self.bin), "LOG_DIR": str(self.tmp), "CALLS": str(self.tmp / "calls"),
                "TIMEOUTS": str(self.tmp / "timeouts"), "STATE": str(self.tmp), "BIN": str(self.bin),
                "MODE": mode or ""}

    def lines(self, name: str) -> list[str]:
        f = self.tmp / name
        return f.read_text().splitlines() if f.exists() else []


class EnsureXzTests(FakeBin, unittest.TestCase):
    """Cua's agent installs unpack Node with `tar -xJf`, which needs xz; Orgo's
    image has none and stale package lists. Without ensure_xz every
    `cua agent ensure` failed with "node: unpack failed"."""

    def setUp(self):
        self.make_bin()

    def run_ensure(self, mode: str | None) -> str:
        harness = "\n".join([
            "set -euo pipefail",
            "changed=()",
            function_source("mark"),
            function_source("note"),
            function_source("apt_xz"),
            function_source("ensure_xz"),
            "ensure_xz",
            'echo "changed=${changed[*]:-}"',
        ])
        res = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=30, env=self.env(mode))
        return res.stdout + res.stderr + f"rc={res.returncode}"

    def test_xz_present_installs_nothing(self):
        self.tool("xz", "#!/bin/sh\nexit 0\n")
        out = self.run_ensure("ok")
        self.assertIn("rc=0", out)
        self.assertIn("changed=\n", out)
        self.assertEqual(self.lines("calls"), [])

    def test_stale_lists_are_refreshed_once_then_xz_installs(self):
        out = self.run_ensure("stale")
        self.assertIn("rc=0", out)
        self.assertIn("changed=xz-utils", out)
        calls = self.lines("calls")
        self.assertEqual([c.split()[1] for c in calls], ["install", "update", "install"])
        self.assertTrue(all(c.startswith("noninteractive ") for c in calls if c.split()[1] == "install"))
        self.assertTrue((self.bin / "xz").exists())
        self.assertIn("404", (self.tmp / "apt.log").read_text())  # apt's output goes to the log, not Orgo
        self.assertNotIn("404", out)
        # Worst case of apt in its own Orgo bash call (see the comment in ensure_xz).
        self.assertLessEqual(sum(map(int, self.lines("timeouts"))), 105)

    def test_working_lists_need_no_refresh(self):
        out = self.run_ensure("ok")
        self.assertIn("rc=0", out)
        self.assertEqual([c.split()[1] for c in self.lines("calls")], ["install"])

    def test_apt_down_is_reported_not_fatal(self):
        out = self.run_ensure("down")
        self.assertIn("rc=0", out)
        self.assertIn("could not install xz-utils", out)
        self.assertIn("changed=\n", out)

    def test_install_without_xz_is_not_success(self):
        out = self.run_ensure("noxz")
        self.assertIn("rc=0", out)
        self.assertIn("could not install xz-utils", out)
        self.assertIn("changed=\n", out)

    def test_no_apt_get_is_reported(self):
        out = self.run_ensure(None)
        self.assertIn("rc=0", out)
        self.assertIn("xz is missing and apt-get is not available", out)


class GuestModeXzTests(FakeBin, unittest.TestCase):
    """The real main_setup, main_agent_deps, ensure_xz and emit_status with
    stand-ins for the downloads, supervisord and Cua. Setup never runs apt (it
    must fit Orgo's 300 s per call); --agent-deps installs xz in its own call;
    a computer without xz still has a working service, with "xz" missing."""

    STUBS = """
preflight() { :; }
platform() { :; }
find_supervisor() { :; }
flock() { :; }
readlink() { echo /old/cua; }  # one change, so no empty list (macOS bash 3.2 under set -u)
ln() { :; }
cmp() { return 0; }
install_archive() { echo "install_archive $1"; }
write_wrapper() { return 1; }
write_program_conf() { return 1; }
sctl() { :; }
stop_detached() { :; }
program_state() { echo RUNNING; }
host_mode() { echo "direct 127.0.0.1:3211"; }
caps_output() { printf 'cua-spacesd 0.5.3\\n- h264_hw\\n'; }
"""

    def setUp(self):
        self.make_bin("python3", "awk", "mkdir", "chmod", "rm")
        self.tool("cua", "#!/bin/sh\necho cua 0.5.0\n")
        host = self.tmp / "host"
        host.mkdir()
        (host / "env-token").write_text("t\n")

    def run_mode(self, main: str, mode: str) -> dict:
        body = function_source(main).replace("/run/orgo-cua.lock", str(self.tmp / "lock"))
        harness = "\n".join([
            "set -euo pipefail",
            "changed=()",
            f"CUA_VERSION=0.5.0 LISTEN=127.0.0.1:3211 PREFIX={self.tmp}/opt HOST_DIR={self.tmp}/host",
            f"CUA_BIN={self.bin}/cua SPACESD_BIN=/x PROGRAM=cua-spacesd PAIR_FILE= PAIR_CODE= UNPAIR= NAME=",
            "SPACESD_VERSION=0.5.3 RELEASES=https://x CLI_PLAT=p CLI_SHA=s SD_PLAT=p SD_SHA=s DISPLAY_NAME=:99",
            *(function_source(f) for f in ("mark", "note", "apt_xz", "ensure_xz", "cua_version", "emit_status", "fail")),
            self.STUBS,
            body,
            main,
        ])
        res = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60, env=self.env(mode))
        self.output = res.stdout + res.stderr
        line = next((x for x in self.output.splitlines() if x.startswith("ORGO_CUA_STATUS ")), None)
        self.assertIsNotNone(line, self.output)
        return json.loads(line.removeprefix("ORGO_CUA_STATUS "))

    def test_setup_never_runs_apt_and_reports_xz_missing(self):
        status = self.run_mode("main_setup", "ok")
        self.assertIn("install_archive cua-spacesd-", self.output)
        self.assertTrue(status["ok"], self.output)
        self.assertEqual(status["missing"], ["h264_hw", "xz"])
        self.assertEqual(self.lines("calls"), [])

    def test_agent_deps_installs_xz(self):
        status = self.run_mode("main_agent_deps", "stale")
        self.assertTrue(status["ok"], self.output)
        self.assertEqual(status["changed"], ["xz-utils"])
        self.assertEqual(status["missing"], ["h264_hw"])

    def test_agent_deps_with_apt_down_leaves_the_service_ok(self):
        status = self.run_mode("main_agent_deps", "down")
        self.assertTrue(status["ok"], self.output)
        self.assertEqual(status["error"], "")
        self.assertEqual(status["missing"], ["h264_hw", "xz"])
        self.assertIn("could not install xz-utils", self.output)

    def test_agent_deps_is_a_mode(self):
        text = SCRIPT.read_text()
        self.assertIn("--agent-deps) mode_arg=agent-deps ;;", text)
        self.assertRegex(text, r'elif \[ "\$mode_arg" = agent-deps \]; then\n    main_agent_deps\n')


class GuestUnpairTests(FakeBin, unittest.TestCase):
    """The real main_setup and run_host_remove with --unpair: Cua's own `cua host
    remove` takes a paired computer out of the user's Cua account (2026-10-24 on
    staging: `cua spaces relay-unregister` refuses a connected computer), then
    the local-only setup comes back. A failed remove keeps the pairing."""

    FAKE_CUA = """#!/bin/sh
echo "$*" >> "$CALLS"
case "$*" in
*"--version"*) echo "cua 0.7.0" ;;
*"host remove --force"*) [ -n "$REMOVE_FAILS" ] && { echo "relay unreachable"; exit 3; }
    echo "none -" > "$STATE/mode"; echo "This machine is no longer set up for access." ;;
esac
"""
    STUBS = """
preflight() { :; }
platform() { :; }
find_supervisor() { :; }
flock() { :; }
readlink() { echo /usr/local/bin/cua-old; }
ln() { :; }
cmp() { return 0; }
install_archive() { :; }
write_wrapper() { return 1; }
write_program_conf() { return 1; }
stop_detached() { :; }
sctl() {
    echo "sctl $*" >> "$CALLS"
    case "$1" in stop) echo STOPPED > "$STATE/service" ;; start) echo RUNNING > "$STATE/service" ;; esac
}
program_state() { cat "$STATE/service"; }
host_mode() { cat "$STATE/mode"; }
caps_output() { printf 'cua-spacesd 0.8.0\\n- h264_hw\\n'; }
run_host_setup() { echo "run_host_setup $*" >> "$CALLS"; echo "direct 127.0.0.1:3211" > "$STATE/mode"; }
"""

    def setUp(self):
        self.make_bin("python3", "awk", "mkdir", "chmod", "rm", "tail", "sed", "cat")
        self.tool("cua", self.FAKE_CUA)
        self.tool("xz", "#!/bin/sh\nexit 0\n")
        host = self.tmp / "host"
        host.mkdir()
        (host / "env-token").write_text("t\n")

    def run_unpair(self, mode: str, remove_fails: bool = False, unpair: bool = True) -> dict:
        (self.tmp / "mode").write_text(mode + "\n")
        (self.tmp / "service").write_text("RUNNING\n")
        body = function_source("main_setup").replace("/run/orgo-cua.lock", str(self.tmp / "lock"))
        harness = "\n".join([
            "set -euo pipefail",
            "changed=()",
            f"CUA_VERSION=0.7.0 LISTEN=127.0.0.1:3211 PREFIX={self.tmp}/opt HOST_DIR={self.tmp}/host",
            f"CUA_BIN={self.bin}/cua SPACESD_BIN=/x PROGRAM=cua-spacesd PAIR_FILE= PAIR_CODE= NAME=n",
            f"UNPAIR={'1' if unpair else ''} LOG_DIR={self.tmp}/log",
            "SPACESD_VERSION=0.8.0 RELEASES=https://x CLI_PLAT=p CLI_SHA=s SD_PLAT=p SD_SHA=s DISPLAY_NAME=:99",
            *(function_source(f) for f in ("mark", "note", "redact", "cua_version", "emit_status", "fail",
                                           "run_host_remove")),
            self.STUBS,
            body,
            "main_setup",
        ])
        env = dict(self.env(None), REMOVE_FAILS="1" if remove_fails else "")
        res = subprocess.run([BASH, "-c", harness], capture_output=True, text=True, timeout=60, env=env)
        self.output = res.stdout + res.stderr
        line = next((x for x in self.output.splitlines() if x.startswith("ORGO_CUA_STATUS ")), None)
        self.assertIsNotNone(line, self.output)
        return json.loads(line.removeprefix("ORGO_CUA_STATUS "))

    def test_a_paired_computer_leaves_cua_then_runs_local_only(self):
        status = self.run_unpair("relay -")
        self.assertTrue(status["ok"], self.output)
        self.assertEqual(status["mode"], "direct")
        self.assertIn("unpaired", status["changed"])
        calls = self.lines("calls")
        remove = calls.index("--embedded host remove --force")
        self.assertIn("sctl stop cua-spacesd", calls[:remove])  # supervisord would restart the relay service
        self.assertIn("run_host_setup --direct 127.0.0.1:3211", calls[remove:])

    def test_a_failed_remove_keeps_the_pairing_and_says_so(self):
        status = self.run_unpair("relay -", remove_fails=True)
        self.assertFalse(status["ok"])
        self.assertEqual(status["mode"], "relay")
        self.assertIn("could not leave the Cua account", status["error"])
        # Review r1 T1: the relay service was stopped for the remove; it must run again.
        self.assertEqual(status["service"], "RUNNING")
        calls = self.lines("calls")
        self.assertIn("sctl start cua-spacesd", calls[calls.index("--embedded host remove --force"):])
        self.assertIn("relay unreachable", self.output)
        self.assertFalse(any(c.startswith("run_host_setup") for c in self.lines("calls")))

    def test_a_local_only_computer_is_left_alone(self):
        status = self.run_unpair("direct 127.0.0.1:3211")
        self.assertTrue(status["ok"], self.output)
        self.assertNotIn("unpaired", status["changed"])
        self.assertFalse(any("host remove" in c or c.startswith("run_host_setup") for c in self.lines("calls")))

    def test_without_unpair_a_paired_computer_stays_paired(self):
        status = self.run_unpair("relay -", unpair=False)
        self.assertEqual(status["mode"], "relay")
        self.assertFalse(any("host remove" in c for c in self.lines("calls")))

    def test_unpair_is_a_flag_that_takes_no_other_mode(self):
        for args in (["--unpair", "--status"], ["--unpair", "--pair-file", "/x"]):
            res = subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 2, args)
            self.assertIn("--unpair takes no other mode", res.stderr)


if __name__ == "__main__":
    unittest.main()
