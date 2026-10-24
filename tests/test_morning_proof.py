"""Tests of the morning proof: scripts/morning_proof.py against a fake Orgo staging API and a
fake `cua` CLI, and scripts/morning-proof.sh end to end (fake `security`, fake `cua`, a local
stand-in for staging). No network, no real keys, no real Cua account."""

import http.server
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import morning_proof  # noqa: E402
import orgo_cua  # noqa: E402
from test_agent_test import ANTHROPIC, OK_STATUS, ORGO_KEY, STAGING, FakeStaging, keypair  # noqa: E402

WRAPPER = ROOT / "scripts" / "morning-proof.sh"
PAIR_CODE = "Q7RM-2XKP-9HDT"
SPACE = "relay:0f0e0d0c0b0a09080706050403020100"


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x06\0\0\0"


# The cua CLI on the maintainer's Mac, in miniature. State lives in files shared with the
# fake Orgo computer: `paired` exists while the computer is in the account, `click` holds
# the last click.
FAKE_CUA = f"""#!/bin/bash
echo "$*" >> "$FAKE_STATE/cua-argv"
case "$*" in
"auth status --json") [ -f "$FAKE_STATE/signed-in" ] && echo '{{"logged_in": true}}' || echo '{{"logged_in": false}}' ;;
"auth login --no-onboarding") : > "$FAKE_STATE/signed-in" ;;
"host pair --json --name {morning_proof.RELAY_NAME} --expires 15")
    echo '{{"code":"{PAIR_CODE}","id":"pc_0123456789abcdef","url":"https://cua.ai/pair/{PAIR_CODE}","install":{{"sh":"curl -fsSL https://cua.ai/pair/{PAIR_CODE} | sh"}}}}' ;;
"spaces ls --json")
    if [ -f "$FAKE_STATE/paired" ] && [ -z "${{FAKE_NEVER_LISTED:-}}" ] || [ -n "${{FAKE_STICKY:-}}" ] && [ -f "$FAKE_STATE/was-paired" ]; then
        echo '{{"spaces":[{{"id":"{SPACE}","name":"{morning_proof.RELAY_NAME}","provider":"relay"}}],"lowDisk":false}}'
    else
        echo '{{"spaces":[],"lowDisk":false}}'
    fi ;;
"do switch {SPACE}") echo "Switched to {SPACE}" ;;
"do screenshot --save "*) python3 -c 'import sys; open(sys.argv[1], "wb").write(bytes.fromhex(sys.argv[2]))' "$4" "{png(1200, 675).hex()}" ;;
"do click "*) echo "$3 $4" > "$FAKE_STATE/click"; echo "clicked ($3, $4)" ;;
*) echo "fake cua: unexpected $*"; exit 2 ;;
esac
"""


# ImageMagick in miniature: records its frame arguments, writes the GIF it was asked for.
FAKE_MAGICK = """#!/bin/bash
echo "$*" >> "$FAKE_STATE/magick-argv"
for last; do :; done
printf 'GIF89a' > "$last"
"""


class MorningStaging(FakeStaging):
    """FakeStaging plus Orgo's screenshot link, the file upload, and a computer that pairs."""

    def __init__(self, priv, pub, state: Path):
        super().__init__(priv, pub)
        self.state = state
        self.polls = ["idle"]
        self.uploads, self.setup_args, self.screenshots = [], [], 0
        self.screenshot_status = 200

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith(STAGING), url
        assert headers.get("Authorization") == f"Bearer {ORGO_KEY}"
        path = url[len(STAGING):]
        self.calls.append((method, path))
        self.bodies.append(body or b"")
        if path.startswith("/storage/"):
            return 200, png(1280, 720)
        if path == "/files/upload":
            self.uploads.append(body)
            return 200, json.dumps({"ok": True}).encode()
        m = re.fullmatch(r"/computers/([^/]+)/screenshot", path)
        if m and m.group(1) in self.desktops:
            self.screenshots += 1
            if self.screenshot_status != 200:
                return self.screenshot_status, json.dumps({"error": "capture failed"}).encode()
            return 200, json.dumps({"success": True, "image": f"/api/storage/shot-{self.screenshots}.png"}).encode()
        status, payload = self.route(method, path, json.loads(body) if body else None)
        return status, json.dumps(payload).encode()

    def guest(self, command):
        paired = self.state / "paired"
        if "setup-cua-host.sh" in command:
            args = command.rsplit("setup-cua-host.sh", 1)[1].split()
            self.setup_args.append(args)
            if args[:1] == ["--pair-file"]:
                paired.touch()
                (self.state / "was-paired").touch()
                status = dict(OK_STATUS, mode="relay", listen="", caps="skipped", changed=["paired"])
            elif args == ["--unpair"]:
                paired.unlink(missing_ok=True)
                status = dict(OK_STATUS, changed=["unpaired", "host"])
            elif args[:1] in (["--status"], ["--status;"]):
                status = dict(OK_STATUS, mode="relay", listen="", caps="skipped") if paired.exists() else OK_STATUS
            else:
                status = OK_STATUS
            return orgo_cua.STATUS_PREFIX + json.dumps(status)
        if command.startswith("rm -f /root/.orgo-cua-pair/"):
            return ""
        if "xmessage" in command:
            return "SCREEN 1280 720\nBUTTON 447 389\n"
        if "getmouselocation" in command:
            click = (self.state / "click").read_text().split() if (self.state / "click").exists() else []
            hit = click == ["419", "365"]
            return f"RC {'7' if hit else 'open'}\nx:447 y:389 screen:0 window:1\n"
        return super().guest(command)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "signed-in").touch()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.cua = bin_dir / "cua"
        self.cua.write_text(FAKE_CUA)
        self.cua.chmod(0o755)
        (bin_dir / "magick").write_text(FAKE_MAGICK)
        (bin_dir / "magick").chmod(0o755)
        priv, pub = keypair(self.tmp)
        self.fake = MorningStaging(priv, pub, self.state)
        self.results = self.tmp / "results"
        self.now = 0.0
        self.saved_env = dict(os.environ)
        os.environ["FAKE_STATE"] = str(self.state)
        os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self.saved_env)))

    def cua_argv(self):
        f = self.state / "cua-argv"
        return f.read_text().splitlines() if f.exists() else []

    def assert_no_secret(self, *texts):
        for secret in (ORGO_KEY, ANTHROPIC, PAIR_CODE):
            for text in texts:
                self.assertNotIn(secret, text)


class HelperTests(Base):
    def run_main(self, **env):
        out, err = io.StringIO(), io.StringIO()

        def sleep(s):
            self.now += s

        env = {"HOME": str(self.tmp), "ORGO_CUA_RESULTS": str(self.results), "ORGO_CUA_TEST_CUA": str(self.cua),
               "ORGO_CUA_TEST_RELAY": "run", **env}
        code = morning_proof.main([], env=env, stdin=io.StringIO(f"{ORGO_KEY}\n{ANTHROPIC}\n"), http=self.fake,
                                  sleep=sleep, clock=lambda: self.now, out=out, err=err, frame_s=0.01)
        morning = self.results / "morning"
        self.result = (morning / "result.txt").read_text()
        self.log = (morning / "log.txt").read_text()
        texts = [out.getvalue(), err.getvalue(), self.result, self.log]
        texts += [p.read_text() for p in morning.rglob("*.txt")] + [p.read_text() for p in morning.rglob("*.json")]
        self.assert_no_secret(*texts)
        for body in self.fake.bodies:
            self.assertNotIn(ORGO_KEY.encode(), body)
            self.assertNotIn(ANTHROPIC.encode(), body)
        for command in self.fake.bodies:  # the pairing code only ever travels as the uploaded file
            if command not in self.fake.uploads:
                self.assertNotIn(PAIR_CODE.encode(), command)
        self.assertFalse(any("other-" in p for _, p in self.fake.calls), "other workspaces are never touched")
        return code

    def assert_cleaned(self):
        self.assertEqual(self.fake.desktops, {})
        self.assertNotIn("ws-scratch", self.fake.workspaces)
        self.assertFalse((self.state / "paired").exists(), "the computer was left in the Cua account")

    def test_pass_end_to_end(self):
        code = self.run_main()
        self.assertEqual(code, 0, self.result + self.log)
        self.assertIn("part 1: PASS", self.result)
        self.assertIn("part 2: PASS", self.result)
        self.assertTrue(self.result.rstrip().endswith("RESULT: PASS"), self.result)
        (frames_dir,) = (self.results / "morning/agent-run").glob("frames-*")
        frames = sorted(frames_dir.glob("*.png"))
        self.assertGreaterEqual(len(frames), 2)  # one while the agent runs, one after
        self.assertEqual(frames[0].read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        (gif,) = (self.results / "morning/agent-run").glob("agent-run-*.gif")
        self.assertEqual(gif.read_bytes(), b"GIF89a")
        self.assertIn(f"GIF {gif}", self.result)
        self.assertEqual(self.cua_argv(), [
            f"host pair --json --name {morning_proof.RELAY_NAME} --expires 15",
            "spaces ls --json",
            f"do switch {SPACE}",
            f"do screenshot --save {self.results / 'morning/relay-cua-screenshot.png'}",
            "do click 419 365",  # 447,389 on the 1280x720 desktop, in Cua's 1200x675 image
            "spaces ls --json",
        ])
        self.assertIn("relay: `cua spaces ls` on this Mac lists it: " + SPACE[:14] + "...", self.result)
        self.assertIn("pressed (xmessage exit 7)", self.result)
        self.assertIn("still lists it: no", self.result)
        pair_args = [a for a in self.fake.setup_args if a[:1] == ["--pair-file"]]
        self.assertEqual(len(pair_args), 1)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertIn(PAIR_CODE.encode(), self.fake.uploads[0])
        self.assertIn(["--unpair"], self.fake.setup_args)
        self.assertTrue((self.results / "morning/relay-orgo-after-click.png").is_file())
        self.assertIn("relay: deleted the scratch workspace", self.result)
        self.assertIn("deleted the scratch workspace", (self.results / "morning/agent-run/result.txt").read_text())
        self.assertIn("relay: computers in other workspaces: before 3, after 3", self.result)
        self.assert_cleaned()

    def test_relay_part_not_run_is_a_fail_and_says_why(self):
        code = self.run_main(ORGO_CUA_TEST_RELAY="skip: the cua CLI is not installed", ORGO_CUA_TEST_CUA="")
        self.assertEqual(code, 1)
        self.assertIn("part 1: PASS", self.result)
        self.assertIn("part 2: not run (skip: the cua CLI is not installed)", self.result)
        self.assertIn("RESULT: FAIL", self.result)
        self.assertEqual(self.cua_argv(), [])
        self.assert_cleaned()

    def test_a_click_that_misses_fails_and_still_leaves_the_account(self):
        real = self.cua.read_text()
        self.cua.write_text(real.replace('echo "$3 $4" > "$FAKE_STATE/click"', 'echo "1 1" > "$FAKE_STATE/click"'))
        code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("did not land", self.result)
        self.assertIn("RESULT: FAIL", self.result)
        self.assertIn(["--unpair"], self.fake.setup_args)  # down took it out of the account
        self.assert_cleaned()

    def test_a_computer_never_listed_fails_and_cleans_up(self):
        os.environ["FAKE_NEVER_LISTED"] = "1"
        code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("lists it: no", self.result)
        self.assertNotIn("do click", "\n".join(self.cua_argv()))
        self.assertGreaterEqual(self.now, morning_proof.LIST_WAIT_S)
        self.assert_cleaned()

    def test_still_listed_after_unpair_is_a_fail(self):
        os.environ["FAKE_STICKY"] = "1"
        code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("still lists it: YES", self.result)

    def test_a_run_without_its_recording_is_a_fail(self):
        # Review r1 S1: the release check asks for the screenshots and the GIF, not just the run.
        self.fake.screenshot_status = 500
        code = self.run_main()
        self.assertEqual(code, 1, self.result)
        self.assertRegex(self.result, r"part 1: FAIL; agent run passed; screenshots 0 saved, [1-9]\d* failed; GIF not made")
        self.assertIn("part 2: PASS", self.result)  # the Orgo screenshot there is evidence only

    def test_no_imagemagick_is_a_fail_that_says_how_to_get_it(self):
        (self.tmp / "bin" / "magick").unlink()
        os.environ["PATH"] = f"{self.tmp / 'bin'}:/usr/bin:/bin"
        code = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("part 1: FAIL; agent run passed", self.result)
        self.assertIn("brew install imagemagick", (self.results / "morning/agent-run/result.txt").read_text())

    def test_each_run_records_into_its_own_frames(self):
        # Review r1 P1: a shorter second run must not pick up the first run's frames.
        self.assertEqual(self.run_main(), 0, self.result)
        self.fake.screenshot_status = 500
        self.run_main()
        self.assertEqual(len(list((self.results / "morning/agent-run").glob("frames-*"))), 1)  # run 2 saved none
        calls = (self.state / "magick-argv").read_text().splitlines()
        self.assertEqual(len(calls), 1)  # run 2 made no GIF from run 1's frames
        self.assertIn("no GIF: no screenshots were saved", (self.results / "morning/agent-run/result.txt").read_text())


class WrapperDryRunTests(Base):
    """scripts/morning-proof.sh end to end with fake `security`, fake `cua` on PATH and a local stand-in."""

    def setUp(self):
        super().setUp()
        security = self.tmp / "bin" / "security"
        security.write_text(
            "#!/bin/sh\n"
            f'case "$*" in *"-s ai.orgo.desktop -a api-key.staging.orgo.ai"*) echo {ORGO_KEY} ;;\n'
            f'*"-s orgo-cua -a anthropic-api-key"*) echo {ANTHROPIC} ;;\n'
            "*) exit 44 ;; esac\n"
        )
        security.chmod(0o755)
        fake = self.fake

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
                status, raw = fake(self.command, STAGING + self.path[len("/api"):], dict(self.headers), body, 5)
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = handle_one

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env = dict(os.environ, PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}", HOME=str(self.tmp),
                        ORGO_CUA_RESULTS=str(self.results), FAKE_STATE=str(self.state),
                        AGENT_TEST_ORGO_BASE=f"http://127.0.0.1:{server.server_port}/api")
        self.env.pop("ORGO_CUA_TEST_CUA", None)

    def wrapper(self, **env):
        res = subprocess.run(["bash", str(WRAPPER)], env={**self.env, **env}, capture_output=True, text=True,
                             timeout=180)
        result_file = self.results / "morning/result.txt"
        result = result_file.read_text() if result_file.exists() else ""
        log = (self.results / "morning/log.txt").read_text() if result_file.exists() else ""
        self.assert_no_secret(res.stdout, res.stderr, result, log)
        return res, result

    def test_full_dry_run_finds_cua_on_path(self):
        res, result = self.wrapper()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr + result)
        self.assertTrue(result.rstrip().endswith("RESULT: PASS"))
        self.assertEqual((self.fake.desktops, list(self.fake.workspaces)), ({}, ["ws-team"]))

    def test_signs_in_first_without_agent_onboarding(self):
        (self.state / "signed-in").unlink()
        res, result = self.wrapper()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr + result)
        self.assertIn("Signing in to Cua", res.stdout)
        self.assertIn("auth login --no-onboarding", self.cua_argv())

    def test_without_the_cua_cli_prints_the_install_line_and_skips_part_two(self):
        self.cua.unlink()
        bare = self.tmp / "nocua"
        bare.mkdir()
        (bare / "security").symlink_to(self.tmp / "bin" / "security")
        (bare / "magick").symlink_to(self.tmp / "bin" / "magick")
        res, result = self.wrapper(PATH=f"{bare}:/usr/bin:/bin:{os.path.dirname(sys.executable)}")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("curl -fsSL https://cua.ai/install.sh | sh -s -- --cli-only", res.stdout)
        self.assertIn("part 2: not run (skip: the cua CLI is not installed)", result)
        self.assertIn("part 1: PASS", result)


if __name__ == "__main__":
    unittest.main()
