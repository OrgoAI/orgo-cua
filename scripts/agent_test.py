"""orgo-cua agent test: one real Cua agent run on an Orgo STAGING computer.

A maintainer runs it through scripts/agent-test.sh, which reads two keys from the
keychain and hands them over on stdin (line 1: the Orgo staging key, line 2:
the Anthropic key). Neither is printed, written to disk or put on a command
line here.

In a new scratch workspace it makes one computer, sets Cua up on it (`orgo-cua
up`), adds the Orgo desktop to Cua as a direct sandbox and installs Claude Code
(no key there yet), then starts one `cua agent run ... claude-code` inside the
computer. Claude Code drives the Orgo desktop through cua-spacesd's desktop
tools: it opens a terminal and types a line that writes a proof file naming the
program it ran under. Then the run is removed, the computer and the workspace
deleted, and results/agent-run/result.txt written (last line RESULT: PASS or
FAIL; ORGO_CUA_RESULTS moves the results folder).

Orgo records every bash command and its output, so the Anthropic key never
travels in one: the computer makes a one-time RSA key pair, this side encrypts
the key to it with `openssl` (the key on its stdin), and the computer decrypts
it into the environment of one `cua agent run` and deletes the private key. The
key then lives inside the computer (the agent's environment and Cua's run
record) until the run is removed and the computer deleted.

Environment (optional): AGENT_TEST_MODEL (a model for Claude Code; default its
own), AGENT_TEST_ORGO_BASE (only http://127.0.0.1:PORT/..., for a dry run
against a local stand-in; the default is Orgo staging).
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, TextIO

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))

import orgo_cua  # noqa: E402
from orgo_cua import ApiError, Orgo, TransportError  # noqa: E402

STAGING = "https://staging.orgo.ai/api"
NAME = "cua-agent-test"
GUEST_SCRIPT = KIT / "guest" / "agent-test.sh"
GUEST_PATH = f"{orgo_cua.GUEST_DIR}/agent-test.sh"
PREFIX = "ORGO_CUA_AGENT "
RUN_TIMEOUT_S = 600
POLL_S = 10
KEYISH = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
MODEL_OK = re.compile(r"[A-Za-z0-9._:-]{1,80}")
LOOPBACK = re.compile(r"http://127\.0\.0\.1:\d+/")

Encrypt = Callable[[str, str], str]


def computers_outside(orgo: Orgo, ws: Optional[str]) -> int:
    """Computers in every workspace but `ws` (counted, never touched)."""
    data = orgo.call("GET", "/workspaces") or {}
    return sum(len(w.get("desktops") or []) for w in data.get("workspaces") or [] if w.get("id") != ws)


def results_dir(env: dict) -> Path:
    """Where the maintainer runs write their results (gitignored)."""
    return Path(env.get("ORGO_CUA_RESULTS") or KIT / "results").expanduser()


def openssl_encrypt(public_pem: str, secret: str) -> str:
    """RSA-OAEP encrypt `secret` to `public_pem` with the openssl CLI; base64 out."""
    if not public_pem.startswith("-----BEGIN PUBLIC KEY-----"):
        raise RuntimeError("the computer did not send a public key")
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:  # a public key only
        f.write(public_pem + "\n")
    try:
        res = subprocess.run(
            ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", f.name, "-pkeyopt", "rsa_padding_mode:oaep"],
            input=secret.encode(), capture_output=True, timeout=30, check=False,
        )
    finally:
        os.unlink(f.name)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("openssl could not encrypt the key")
    return base64.b64encode(res.stdout).decode()


def parse(output: str) -> Optional[dict]:
    for line in reversed((output or "").splitlines()):
        if line.startswith(PREFIX):
            try:
                value = json.loads(line[len(PREFIX):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


class AgentTest:
    def __init__(self, base: str, http: orgo_cua.Http, orgo_key: str, anthropic_key: str, out_dir: Path,
                 encrypt: Encrypt, sleep: Callable[[float], None], clock: Callable[[], float], echo: TextIO,
                 model: Optional[str] = None):
        self.orgo = Orgo(base, orgo_key, http)
        self.anthropic_key, self.out_dir, self.encrypt = anthropic_key, out_dir, encrypt
        self.hidden = [orgo_key, anthropic_key]
        self.sleep, self.clock, self.echo, self.model = sleep, clock, echo, model
        out_dir.mkdir(parents=True, exist_ok=True)
        self.result = out_dir / "result.txt"
        self.logfile = out_dir / "log.txt"
        self.result.write_text("")
        self.logfile.write_text("")
        self.cid: Optional[str] = None

    def scrub(self, text: str) -> str:
        for secret in self.hidden:
            text = text.replace(secret, "<key>")
        return orgo_cua.redact(KEYISH.sub("<redacted>", text))

    def log(self, text: str) -> None:
        with self.logfile.open("a") as f:
            f.write(self.scrub(text).rstrip("\n") + "\n")

    def say(self, text: str) -> None:
        text = self.scrub(text)
        print(text, file=self.echo)
        with self.result.open("a") as f:
            f.write(text + "\n")

    def others(self, ws: Optional[str]) -> int:
        return computers_outside(self.orgo, ws)

    def run_started(self) -> None:
        """Called once the agent run has started (a subclass may watch the screen)."""

    def run_ended(self) -> None:
        """Called after the run is finished or abandoned."""

    def guest(self, args: list[str], install: bool = False) -> dict:
        run = " ".join([GUEST_PATH, *map(shlex.quote, args)])
        if install:
            b64 = base64.b64encode(GUEST_SCRIPT.read_bytes()).decode()
            run = (f"mkdir -p {orgo_cua.GUEST_DIR} && printf %s '{b64}' | base64 -d > {GUEST_PATH}.new"
                   f" && chmod 0755 {GUEST_PATH}.new && mv -f {GUEST_PATH}.new {GUEST_PATH} && {run}")
        assert self.cid
        output = str(self.orgo.bash(self.cid, run, orgo_cua.SETUP_TIMEOUT_S).get("output") or "")
        self.log(f"$ agent-test.sh {args[0]}\n{output}")
        return parse(output) or {"ok": False, "error": "no status line from the computer"}

    def run(self) -> bool:
        before = self.others(None)
        ws = self.orgo.call("POST", "/workspaces", {"name": f"cua-agent-test-{time.strftime('%Y%m%d-%H%M%S')}"})["id"]
        (self.out_dir / "workspace.txt").write_text(ws + "\n")
        self.say(f"scratch workspace: {ws}")
        passed = False
        try:
            passed = self.steps(ws)
        except (ApiError, TransportError, RuntimeError) as e:
            self.say(f"stopped: {e}")
        finally:
            cleaned = self.cleanup(ws)
            try:
                self.say(f"computers in other workspaces: before {before}, after {self.others(ws)}")
            except (ApiError, TransportError) as e:
                self.say(f"could not recount other workspaces: {e}")
        ok = passed and cleaned
        self.say("RESULT: PASS" if ok else f"RESULT: FAIL (see {self.logfile})")
        return ok

    def steps(self, ws: str) -> bool:
        buf = io.StringIO()
        self.cli = orgo_cua.Cli(self.orgo, ws, orgo_cua.State(self.out_dir / "state.json"), buf, buf,
                                self.sleep, self.clock)
        t = self.clock()
        rc = self.cli.up(NAME, 4, 1, None, orgo_cua.SCRIPT_PATH.read_text())
        self.log(f"$ orgo-cua up --name {NAME}\n{buf.getvalue()}")
        entry = self.cli.state.get(self.cli.key(NAME))
        self.cid = entry["id"] if entry else None
        self.say(f"up (Cua host on the Orgo desktop): exit {rc} after {self.clock() - t:.0f} s")
        if rc != 0 or not self.cid:
            return False

        t = self.clock()
        prep = self.guest(["prepare"], install=True)
        if not prep.get("ok") and "timed out" in str(prep.get("error")):
            prep = self.guest(["prepare"])  # the install resumes from what it fetched
        self.say(f"prepare (add the desktop to Cua, install Claude Code): "
                 f"{'ok' if prep.get('ok') else 'failed: ' + str(prep.get('error'))} after {self.clock() - t:.0f} s")
        if not prep.get("ok"):
            return False

        nonce = "orgo-cua-agent-" + secrets.token_hex(4)
        cipher = self.encrypt(str(prep.get("pub") or ""), self.anthropic_key)
        start = self.guest(["start", cipher, nonce, *([self.model] if self.model else [])])
        run_id = str(start.get("run_id") or "")
        self.say(f"start (cua agent run ... claude-code): "
                 f"{'run ' + run_id if start.get('ok') and run_id else 'failed: ' + str(start.get('error'))}")
        if not (start.get("ok") and run_id):
            return False

        self.run_started()
        try:
            t = self.clock()
            while True:
                status = self.guest(["poll", run_id])
                timed_out = not status.get("ok") or status.get("status") in ("running", "unknown")
                if not timed_out or self.clock() - t >= RUN_TIMEOUT_S:
                    break
                self.sleep(POLL_S)
            done = self.guest(["finish", run_id])
        finally:
            self.run_ended()
        self.say(f"run: {done.get('status') or '?'}{' (timed out, stopped)' if timed_out else ''} after "
                 f"{self.clock() - t:.0f} s; turn {done.get('turn')}, tool calls {done.get('tool_calls')}, "
                 f"stop {done.get('stop_reason')}, error {done.get('run_error') or 'none'}")
        if done.get("text"):
            self.say(f"agent reply: {str(done['text'])[:200]!r}")
        expected = f"{nonce} xfce4-terminal"
        proof = str(done.get("proof") or "")
        self.say(f"proof file: {proof!r} (expected {expected!r}: typed into a desktop terminal)")
        self.say(f"Cua run record (holds the key) deleted: {done.get('removed', 'no')}")
        return (not timed_out and done.get("status") == "idle" and not done.get("run_error")
                and proof.strip() == expected)

    def cleanup(self, ws: str) -> bool:
        if hasattr(self, "cli"):
            try:
                buf = io.StringIO()
                self.cli.out = self.cli.err = buf
                rc = self.cli.down(NAME)
                self.log(f"$ orgo-cua down --name {NAME}\n{buf.getvalue()}")
                self.say(f"down: exit {rc}")
            except (ApiError, TransportError) as e:
                self.say(f"down failed: {e}")
        left = -1
        for _ in range(36):
            try:
                desktops = self.orgo.call("GET", f"/workspaces/{ws}").get("desktops") or []
                left = sum(1 for d in desktops if d.get("status") != "deleted")
            except (ApiError, TransportError) as e:
                self.log(f"workspace read: {e}")
            if left == 0:
                break
            self.sleep(5)
        self.say(f"computers left in the scratch workspace: {left}")
        if left != 0:
            self.say("kept the scratch workspace: it still has computers; delete them in Orgo, then the workspace.")
            return False
        try:
            self.orgo.call("POST", f"/workspaces/{ws}/delete")
        except (ApiError, TransportError) as e:
            self.say(f"delete scratch workspace failed: {e}")
            return False
        self.say("deleted the scratch workspace")
        (self.out_dir / "workspace.txt").unlink(missing_ok=True)
        return True


def main(argv: Optional[list[str]] = None, env: Optional[dict] = None, stdin: TextIO = sys.stdin,
         http: orgo_cua.Http = orgo_cua.urllib_http, encrypt: Encrypt = openssl_encrypt,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    env = dict(os.environ if env is None else env)
    p = argparse.ArgumentParser(prog="agent-test", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.parse_args(sys.argv[1:] if argv is None else argv)
    base = env.get("AGENT_TEST_ORGO_BASE") or STAGING
    if base != STAGING and not LOOPBACK.match(base):
        print("Staging only: AGENT_TEST_ORGO_BASE may only name a local stand-in (http://127.0.0.1:PORT/...).", file=err)
        return 2
    model = env.get("AGENT_TEST_MODEL") or None
    if model and not MODEL_OK.fullmatch(model):
        print("AGENT_TEST_MODEL may hold only letters, digits and . _ : -", file=err)
        return 2
    lines = [line.strip() for line in stdin.read().splitlines()]
    if len(lines) < 2 or not lines[0] or not lines[1]:
        print("Expected two lines on stdin: the Orgo staging key, then the Anthropic key.", file=err)
        return 2
    out_dir = results_dir(env) / "agent-run"
    test = AgentTest(base, http, lines[0], lines[1], out_dir, encrypt, sleep, clock, out, model)
    # A closed terminal or a kill still runs the cleanup.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(1))
    try:
        return 0 if test.run() else 1
    except (ApiError, TransportError) as e:
        test.say(f"Orgo API error before anything was made: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
