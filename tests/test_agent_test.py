"""Tests of the agent test: guest/agent-test.sh (bash, real openssl, a fake `cua`),
scripts/agent_test.py (a fake Orgo staging API) and scripts/agent-test.sh (a full dry
run against a local stand-in, with a fake `security`). No network, no real keys."""

import base64
import http.server
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import agent_test  # noqa: E402
import orgo_cua  # noqa: E402

GUEST = ROOT / "guest" / "agent-test.sh"
WRAPPER = ROOT / "scripts" / "agent-test.sh"
ORGO_KEY = "orgo_staging_fake_key_0123"
ANTHROPIC = "sk-ant-api03-FAKEfake_0123456789-abcdefghijklmnopqrstuvwxyzABCDEFGH"  # secscan:allow fake fixture
TOKEN = "0123456789abcdef" * 3  # the shape of a spacesd env token
STAGING = agent_test.STAGING
OK_STATUS = {"ok": True, "cua": "0.5.0", "spacesd": "0.5.3", "mode": "direct", "listen": "127.0.0.1:3211",
             "service": "RUNNING", "caps": "ok", "missing": [], "changed": [], "error": ""}


def keypair(folder: Path) -> tuple[Path, str]:
    priv = folder / "test-priv.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(priv)],
                   check=True, capture_output=True)
    pub = subprocess.run(["openssl", "pkey", "-in", str(priv), "-pubout"], check=True, capture_output=True, text=True)
    return priv, pub.stdout.strip()


def decrypt(priv: Path, cipher_b64: str) -> str:
    res = subprocess.run(["openssl", "pkeyutl", "-decrypt", "-inkey", str(priv), "-pkeyopt", "rsa_padding_mode:oaep"],
                         input=base64.b64decode(cipher_b64), check=True, capture_output=True)
    return res.stdout.decode()


def line(**fields) -> str:
    return agent_test.PREFIX + json.dumps({"ok": not fields.get("error"), "error": "", **fields})


class FakeStaging:
    """Orgo staging in memory, with a fake computer that answers like the guest scripts."""

    def __init__(self, priv: Path, pub: str):
        self.priv, self.pub = priv, pub
        self.others = [{"id": f"other-{i}", "name": f"theirs-{i}", "status": "running"} for i in range(3)]
        self.workspaces = {"ws-team": {"id": "ws-team", "name": "Team"}}
        self.desktops = {}
        self.calls, self.bodies, self.guest_calls, self.decrypted = [], [], [], []
        self.prepare_error, self.proof_suffix = "", "xfce4-terminal"
        self.polls = ["running", "idle"]
        self.fail_poll = False
        self.nonce = None

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith(STAGING), url
        assert headers.get("Authorization") == f"Bearer {ORGO_KEY}"
        path = url[len(STAGING):]
        self.calls.append((method, path))
        self.bodies.append(body or b"")
        status, payload = self.route(method, path, json.loads(body) if body else None)
        return status, json.dumps(payload).encode()

    def listing(self, ws):
        if ws == "ws-team":
            return self.others
        return [d for d in self.desktops.values() if d["ws"] == ws]

    def route(self, method, path, data):
        if path == "/workspaces" and method == "GET":
            return 200, {"workspaces": [dict(w, desktops=self.listing(w["id"])) for w in self.workspaces.values()]}
        if path == "/workspaces" and method == "POST":
            self.workspaces["ws-scratch"] = {"id": "ws-scratch", "name": data["name"]}
            return 201, {"id": "ws-scratch"}
        m = re.fullmatch(r"/workspaces/([^/]+)(/delete)?", path)
        if m and m.group(1) in self.workspaces:
            if m.group(2):
                assert not self.listing(m.group(1))
                del self.workspaces[m.group(1)]
                return 200, {"success": True}
            return 200, dict(self.workspaces[m.group(1)], desktops=self.listing(m.group(1)))
        if path == "/computers" and method == "POST":
            cid = f"cid-{len(self.desktops) + 1}"
            self.desktops[cid] = {"id": cid, "name": data["name"], "status": "running", "ws": data["workspace_id"]}
            return 201, self.desktops[cid]
        m = re.fullmatch(r"/computers/([^/]+)(/\w+)?", path)
        comp = self.desktops.get(m.group(1)) if m else None
        if comp is None:
            return 404, {"error": "not found"}
        if m.group(2) is None:
            if method == "DELETE":
                del self.desktops[comp["id"]]
                return 200, {"success": True}
            return 200, comp
        if m.group(2) == "/bash":
            if data["command"] == "true":
                return 200, {"output": "", "exit_code": 0}
            if self.fail_poll and "agent-test.sh poll" in data["command"]:
                return 500, {"error": "desktop-api unavailable"}
            return 200, {"output": self.guest(data["command"]), "exit_code": 0}
        return 404, {"error": "no route"}

    def guest(self, command):
        if "setup-cua-host.sh" in command:
            return orgo_cua.STATUS_PREFIX + json.dumps(OK_STATUS)
        args = shlex.split(command.rsplit("/opt/orgo-cua/agent-test.sh ", 1)[1])
        self.guest_calls.append(args)
        sent = re.search(r"printf %s '([A-Za-z0-9+/=]+)' \| base64 -d", command)
        assert sent is None or base64.b64decode(sent.group(1)) == GUEST.read_bytes()
        if args[0] == "prepare":
            return line(error=self.prepare_error) if self.prepare_error else line(pub=self.pub)
        if args[0] == "start":
            self.decrypted.append(decrypt(self.priv, args[1]))
            self.nonce = args[2]
            return "note: installing\n" + line(run_id="run-1a2b3c4d")
        if args[0] == "poll":
            return line(status=self.polls.pop(0) if len(self.polls) > 1 else self.polls[0])
        if args[0] == "finish":
            return "--- agent events\nTool screenshot\n--- end of agent events\n" + line(
                status=self.polls[-1], turn=1, tool_calls=4, stop_reason="end_turn", run_error=None,
                text="DONE", proof=f"{self.nonce} {self.proof_suffix}", removed="yes")
        raise AssertionError(args)


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        priv, pub = keypair(self.tmp)
        self.fake = FakeStaging(priv, pub)
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def run_main(self, stdin=f"{ORGO_KEY}\n{ANTHROPIC}\n", **env):
        out, err = io.StringIO(), io.StringIO()
        code = agent_test.main([], env={"HOME": str(self.tmp), "ORGO_CUA_RESULTS": str(self.tmp / "results"), **env},
                               stdin=io.StringIO(stdin), http=self.fake,
                               sleep=self.sleep, clock=self.clock, out=out, err=err)
        result_file = self.tmp / "results/agent-run/result.txt"
        log_file = result_file.with_name("log.txt")
        self.result = result_file.read_text() if result_file.exists() else ""
        texts = out.getvalue() + err.getvalue() + self.result + (log_file.read_text() if log_file.exists() else "")
        for secret in (ORGO_KEY, ANTHROPIC):
            self.assertNotIn(secret, texts)
            for body in self.fake.bodies:  # never in a bash command or any other request body
                self.assertNotIn(secret.encode(), body)
        self.assertFalse(any(p.startswith("/files/") for _, p in self.fake.calls), "the key is never uploaded")
        self.assertFalse(any("other-" in p for _, p in self.fake.calls), "other workspaces are never touched")
        return code, err.getvalue()

    def assert_cleaned(self):
        self.assertEqual(self.fake.desktops, {})
        self.assertNotIn("ws-scratch", self.fake.workspaces)
        self.assertIn("deleted the scratch workspace", self.result)
        self.assertIn("computers in other workspaces: before 3, after 3", self.result)

    def test_pass_end_to_end(self):
        code, _ = self.run_main()
        self.assertEqual(code, 0, self.result)
        self.assertIn("RESULT: PASS", self.result)
        self.assertEqual(self.fake.decrypted, [ANTHROPIC])
        self.assertEqual([a[0] for a in self.fake.guest_calls], ["prepare", "start", "poll", "poll", "finish"])
        self.assertIn(f"proof file: '{self.fake.nonce} xfce4-terminal'", self.result)
        self.assertIn("Cua run record (holds the key) deleted: yes", self.result)
        self.assert_cleaned()

    def test_the_agent_using_its_own_shell_fails(self):
        self.fake.proof_suffix = "bash"
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("RESULT: FAIL", self.result)
        self.assert_cleaned()

    def test_a_failed_prepare_never_sends_the_key(self):
        self.fake.prepare_error = "installing claude-code failed or timed out (exit 124)"
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual([a[0] for a in self.fake.guest_calls], ["prepare", "prepare"])  # one retry after a timeout
        self.assertEqual(self.fake.decrypted, [])
        self.assert_cleaned()

    def test_an_api_error_mid_run_still_cleans_up(self):
        self.fake.fail_poll = True
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("stopped: Orgo API 500", self.result)
        self.assert_cleaned()

    def test_a_run_that_never_ends_is_stopped(self):
        self.fake.polls = ["running"]
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("(timed out, stopped)", self.result)
        self.assertEqual(self.fake.guest_calls[-1][0], "finish")
        self.assertGreaterEqual(self.now, agent_test.RUN_TIMEOUT_S)
        self.assert_cleaned()

    def test_staging_only(self):
        for base in ("https://www.orgo.ai/api", "https://staging.orgo.ai.evil.test/api", "http://10.0.0.5:80/api"):
            code, err = self.run_main(AGENT_TEST_ORGO_BASE=base)
            self.assertEqual(code, 2, base)
            self.assertIn("Staging only", err)
        self.assertEqual(self.fake.calls, [])

    def test_needs_both_keys_on_stdin(self):
        code, err = self.run_main(stdin=f"{ORGO_KEY}\n")
        self.assertEqual(code, 2)
        self.assertIn("Expected two lines", err)
        self.assertEqual(self.fake.calls, [])


class GuestScriptTests(unittest.TestCase):
    """The real guest script under bash, with real openssl and a fake `cua`."""

    FAKE_CUA = """#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_ARGV"
printf '%s|%s\\n' "${CUA_ENV_TOKEN:-}" "${ANTHROPIC_API_KEY:-}" >> "$FAKE_ENV"
case "$*" in
*"agent run"*)
    [ -z "${FAKE_ECHO_KEY:-}" ] || echo "error: the key $ANTHROPIC_API_KEY was refused"
    echo '{"run_id":"run-1a2b3c4d","harness":"claude-code"}' ;;
*"agent status"*)
    echo '{"status":{"status":"idle","phase":"done"},"result":{"turn":1,"text":"DONE","stop_reason":"end_turn","error":null,"tool_calls":4}}' ;;
*"agent logs"*)
    echo 'Tool screenshot'; echo 'Tool type_text'
    [ -z "${FAKE_LOG_KEY:-}" ] || echo "env ANTHROPIC_API_KEY=$FAKE_LOG_KEY" ;;
*) echo ok ;;
esac
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        cua = self.tmp / "cua"
        cua.write_text(self.FAKE_CUA)
        cua.chmod(cua.stat().st_mode | stat.S_IXUSR)
        (self.tmp / "token").write_text(TOKEN + "\n")
        self.work = self.tmp / "work"
        self.proof = self.tmp / "proof.txt"
        # Cua unpacks Node with xz; a stand-in, so the tests need no real one.
        xz = self.tmp / "xzbin" / "xz"
        xz.parent.mkdir()
        xz.write_text("#!/bin/sh\nexit 0\n")
        xz.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{xz.parent}:{os.environ['PATH']}",
                        ORGO_CUA_BIN=str(cua), ORGO_CUA_AGENT_DIR=str(self.work),
                        ORGO_CUA_TOKEN_FILE=str(self.tmp / "token"), ORGO_CUA_PROOF=str(self.proof),
                        FAKE_ARGV=str(self.tmp / "argv.log"), FAKE_ENV=str(self.tmp / "env.log"))

    def guest(self, *args, **env):
        res = subprocess.run(["bash", str(GUEST), *args], env={**self.env, **env}, capture_output=True, text=True,
                             timeout=60)
        status = agent_test.parse(res.stdout)
        self.assertIsNotNone(status, res.stdout + res.stderr)
        self.assertNotIn(ANTHROPIC, res.stdout + res.stderr)
        self.assertNotIn(TOKEN, res.stdout + res.stderr)
        return status, res

    def argv_log(self):
        text = (self.tmp / "argv.log").read_text()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(ANTHROPIC, text)
        return text

    def test_prepare_adds_the_desktop_and_makes_a_key_pair(self):
        status, res = self.guest("prepare")
        self.assertTrue(status["ok"], res.stdout)
        self.assertTrue(status["pub"].startswith("-----BEGIN PUBLIC KEY-----"))
        self.assertEqual(stat.S_IMODE((self.work / "agent.key").stat().st_mode), 0o600)
        argv = self.argv_log()
        self.assertIn("--embedded sb create --on direct:127.0.0.1:3211 --name orgo-desk", argv)
        self.assertIn("--embedded agent ensure orgo-desk claude-code", argv)
        self.assertTrue(all(e.startswith(TOKEN + "|") for e in (self.tmp / "env.log").read_text().splitlines()))

    def test_prepare_without_xz_stops_before_cua(self):
        bare = self.tmp / "bare"
        bare.mkdir()
        for name in ("bash", "python3", "sed"):
            (bare / name).symlink_to(shutil.which(name))
        status, _ = self.guest("prepare", PATH=str(bare))
        self.assertFalse(status["ok"])
        self.assertIn("xz is missing", status["error"])
        self.assertFalse((self.tmp / "argv.log").exists())  # no cua command ran
        self.assertFalse(self.work.exists())  # no key pair

    def test_start_decrypts_the_key_into_the_run_environment_only(self):
        status, _ = self.guest("prepare")
        cipher = agent_test.openssl_encrypt(status["pub"], ANTHROPIC)
        status, res = self.guest("start", cipher, "nonce-1")
        self.assertTrue(status["ok"], res.stdout)
        self.assertEqual(status["run_id"], "run-1a2b3c4d")
        self.assertFalse((self.work / "agent.key").exists())
        run = self.argv_log().split("--json agent run ", 1)[1]  # the prompt spans lines
        self.assertTrue(run.startswith("orgo-desk claude-code This is a test of desktop control"), run)
        self.assertIn("--env-from-host ANTHROPIC_API_KEY --exit-when-idle", run)
        self.assertIn(f"echo nonce-1 $(ps -o comm= -p $PPID) > {self.proof}", run)
        envs = (self.tmp / "env.log").read_text().splitlines()
        self.assertEqual(envs[-1], f"{TOKEN}|{ANTHROPIC}")  # the run, and only the run, has the key
        self.assertTrue(all(e == f"{TOKEN}|" for e in envs[:-1]))

    def test_the_prompt_asks_for_foreground_input(self):
        # 2026-10-22: the agent's background click and Enter never landed on Orgo's
        # desktop (Xtigervnc drops them for GTK apps); it had to retry in foreground.
        status, _ = self.guest("prepare")
        self.guest("start", agent_test.openssl_encrypt(status["pub"], ANTHROPIC), "nonce-1")
        run = self.argv_log().split("--json agent run ", 1)[1]
        self.assertIn('Use delivery_mode "foreground" for every click and key press', run)

    def test_a_key_echoed_by_cua_is_scrubbed(self):
        status, _ = self.guest("prepare")
        cipher = agent_test.openssl_encrypt(status["pub"], ANTHROPIC)
        status, res = self.guest("start", cipher, "nonce-1", FAKE_ECHO_KEY="1")
        self.assertTrue(status["ok"])
        self.assertIn("the key <redacted> was refused", res.stdout)

    def test_a_bad_cipher_never_starts_a_run(self):
        self.guest("prepare")
        status, _ = self.guest("start", base64.b64encode(b"x" * 256).decode(), "nonce-1")
        self.assertEqual((status["ok"], status["error"]), (False, "could not decrypt the key"))
        self.assertFalse((self.work / "agent.key").exists())
        self.assertNotIn("agent run", self.argv_log())

    def test_finish_reports_the_proof_and_removes_the_run(self):
        self.work.mkdir()
        self.proof.write_text("nonce-1 xfce4-terminal\n")
        status, res = self.guest("finish", "run-1a2b3c4d")
        self.assertEqual((status["status"], status["proof"], status["removed"], status["tool_calls"]),
                         ("idle", "nonce-1 xfce4-terminal", "yes", 4))
        self.assertIn("Tool type_text", res.stdout)
        self.assertIn("--embedded agent rm orgo-desk run-1a2b3c4d", self.argv_log())
        self.assertFalse(self.work.exists())

    def test_finish_scrubs_key_shaped_text_from_the_events(self):
        # The key is no longer known here, so its shape is what is scrubbed.
        _, res = self.guest("finish", "run-1a2b3c4d", FAKE_LOG_KEY=ANTHROPIC)
        self.assertIn("env ANTHROPIC_API_KEY=<redacted>", res.stdout)


class WrapperDryRunTests(unittest.TestCase):
    """scripts/agent-test.sh end to end: fake `security`, the real helper, a local stand-in for staging."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        security = bin_dir / "security"
        security.write_text(
            "#!/bin/sh\n"
            f'case "$*" in *"-s ai.orgo.desktop"*) echo {ORGO_KEY} ;;\n'
            f'*"-s orgo-cua -a anthropic-api-key"*) [ -n "$NO_ANTHROPIC" ] && exit 44; echo {ANTHROPIC} ;;\n'
            "*) exit 44 ;; esac\n"
        )
        security.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(self.tmp), NO_ANTHROPIC="",
                        ORGO_CUA_RESULTS=str(self.tmp / "results"))

    def wrapper(self, *args, **env):
        return subprocess.run(["bash", str(WRAPPER), *args], env={**self.env, **env}, capture_output=True,
                              text=True, timeout=120)

    def test_help_reads_no_keys(self):
        res = self.wrapper("--help", PATH="/usr/bin:/bin:" + os.path.dirname(sys.executable))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("one real Cua agent run on an Orgo STAGING computer", res.stdout)

    def test_stops_without_the_anthropic_key(self):
        res = self.wrapper(NO_ANTHROPIC="1")
        self.assertEqual(res.returncode, 2)
        self.assertIn("No Anthropic key found", res.stdout)

    def test_full_dry_run_against_a_local_stand_in(self):
        priv, pub = keypair(self.tmp)
        fake = FakeStaging(priv, pub)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
                status, raw = fake(self.command, STAGING + self.path[len("/api"):], dict(self.headers), body, 5)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = handle_one

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        fake.polls = ["idle"]
        res = self.wrapper(AGENT_TEST_ORGO_BASE=f"http://127.0.0.1:{server.server_port}/api")
        result = (self.tmp / "results/agent-run/result.txt").read_text()
        log = (self.tmp / "results/agent-run/log.txt").read_text()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr + result)
        self.assertIn("RESULT: PASS", result)
        self.assertEqual(fake.decrypted, [ANTHROPIC])
        for secret in (ORGO_KEY, ANTHROPIC):
            self.assertNotIn(secret, res.stdout + res.stderr + result + log)
            for body in fake.bodies:
                self.assertNotIn(secret.encode(), body)
        self.assertEqual((fake.desktops, list(fake.workspaces)), ({}, ["ws-team"]))


if __name__ == "__main__":
    unittest.main()
