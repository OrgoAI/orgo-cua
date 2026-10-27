"""orgo-cua morning proof: the two real end-to-end runs before a release, on Orgo STAGING.

A maintainer runs it through scripts/morning-proof.sh, which reads the Orgo staging key
and the Anthropic key from the keychain and hands them over on stdin (line 1: Orgo,
line 2: Anthropic). Neither is printed, written to disk or put on a command line.

1. Agent: scripts/agent_test.py's run (Claude Code, started with `cua agent run`, drives
   the Orgo desktop), with an Orgo screenshot about every 2 s in agent-run/frames-<time>/
   and a GIF of them, agent-run/agent-run-<time>.gif (ImageMagick's `magick`). The part
   passes only with the recording: at least two screenshots and the GIF.
2. Relay, the real Cua user path, with the `cua` CLI on this Mac signed in to the
   maintainer's cua.ai account: `cua host pair` makes a one-time code; `orgo-cua up
   --pair` joins a new Orgo computer to the account with it (the code travels as a file,
   never in a command); `cua spaces ls` must list the computer; one `cua do screenshot`
   and one `cua do click` from this Mac press a button on the Orgo desktop (checked inside
   the computer and with an Orgo screenshot); `orgo-cua unpair` takes it out of the
   account (`cua spaces ls` must stop listing it); `orgo-cua down` deletes it.

Each part makes its own scratch workspace and deletes it; computers in other workspaces
are counted, never touched. Results: results/morning/ (ORGO_CUA_RESULTS moves it):
result.txt, last line RESULT: PASS or FAIL, and log.txt, with the keys, the pairing code
and token-shaped text scrubbed.

Environment: ORGO_CUA_TEST_CUA (the cua CLI; empty skips the relay part),
ORGO_CUA_TEST_RELAY ("run", or "skip: <reason>" from the wrapper), AGENT_TEST_MODEL and
AGENT_TEST_ORGO_BASE as for agent_test.py.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, TextIO

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / "scripts"))

import agent_test  # noqa: E402
import orgo_cua  # noqa: E402
from orgo_cua import ApiError, Orgo, TransportError  # noqa: E402

RELAY_NAME = "cua-morning-relay"
FRAME_S = 2.0
LIST_WAIT_S = 90
SCREEN = (1280, 720)
# A button on the Orgo desktop; xmessage exits 7 when it is pressed. xwininfo's tree shows
# the button as the lowest child window of button size; this prints its centre.
BUTTON_RC = "/tmp/orgo-cua-click.rc"
BUTTON_COMMAND = (
    f"export DISPLAY=:99; rm -f {BUTTON_RC}; "
    "setsid -f bash -c 'xmessage -geometry +400+300 -buttons \"Press me:7\" \"Cua clicks this from your Mac\"; "
    f"echo $? > {BUTTON_RC}' >/dev/null 2>&1; "
    "for i in $(seq 20); do W=$(xdotool search --name '^xmessage$' | tail -n 1); [ -n \"$W\" ] && break; sleep 0.5; done; "
    "xdpyinfo | awk '/dimensions:/ { split($2, d, \"x\"); print \"SCREEN\", d[1], d[2] }'; "
    "xwininfo -tree -id \"$W\" | awk '{ for (i = 1; i < NF; i++) "
    "if ($i ~ /^[0-9]+x[0-9]+[+-]/ && $(i + 1) ~ /^[+][0-9]+[+][0-9]+$/) { split($i, g, /[x+]/); "
    "split($(i + 1), a, /[+]/); if (g[1] > 30 && g[2] >= 15 && g[2] <= 40 && a[3] + 0 >= by) "
    "{ by = a[3]; bx = a[2]; bw = g[1]; bh = g[2] } } } "
    "END { if (bw) printf \"BUTTON %d %d\\n\", bx + bw / 2, by + bh / 2 }'"
)
CHECK_COMMAND = f"sleep 1; echo \"RC $(cat {BUTTON_RC} 2>/dev/null || echo open)\"; DISPLAY=:99 xdotool getmouselocation"


def png_size(path: Path) -> Optional[tuple[int, int]]:
    head = path.read_bytes()[:24] if path.is_file() else b""
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        return None
    return struct.unpack(">II", head[16:24])


def image_suffix(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".img"


class Results:
    """result.txt (what happened) and log.txt (details), both scrubbed of every secret."""

    def __init__(self, out_dir: Path, echo: TextIO, hidden: list[str]):
        self.out_dir, self.echo, self.hidden = out_dir, echo, hidden
        out_dir.mkdir(parents=True, exist_ok=True)
        self.result, self.logfile = out_dir / "result.txt", out_dir / "log.txt"
        self.result.write_text("")
        self.logfile.write_text("")

    def scrub(self, text: str) -> str:
        for secret in self.hidden:
            if secret:
                text = text.replace(secret, "<secret>")
        return orgo_cua.redact(agent_test.KEYISH.sub("<redacted>", text))

    def log(self, text: str) -> None:
        with self.logfile.open("a") as f:
            f.write(self.scrub(text).rstrip("\n") + "\n")

    def say(self, text: str) -> None:
        text = self.scrub(text)
        print(text, file=self.echo)
        with self.result.open("a") as f:
            f.write(text + "\n")


class FramedAgentTest(agent_test.AgentTest):
    """The agent test, with an Orgo screenshot about every FRAME_S seconds while the agent runs.
    Each run records into its own frames-<time>/ folder, so an older run's frames never end up
    in this run's GIF."""

    def __init__(self, *args, frame_s: float = FRAME_S, **kw):
        super().__init__(*args, **kw)
        self.stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.frames = self.out_dir / f"frames-{self.stamp}"
        self.gif = self.out_dir / f"agent-run-{self.stamp}.gif"
        self.frame_s = frame_s
        self.saved = self.failed = 0
        self.gif_made = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def grab(self) -> None:
        assert self.cid
        try:
            data = self.orgo.screenshot(self.cid)
        except (ApiError, TransportError) as e:
            self.failed += 1
            self.log(f"screenshot failed: {e}")
            return
        self.frames.mkdir(parents=True, exist_ok=True)
        self.saved += 1
        (self.frames / f"{self.saved:04d}{image_suffix(data)}").write_bytes(data)

    def _watch(self) -> None:
        while True:
            self.grab()
            if self._stop.wait(self.frame_s):
                break

    def run_started(self) -> None:
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def run_ended(self) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=120)
            self.grab()  # the screen after the run
        self.say(f"screenshots during the run: {self.saved} saved, {self.failed} failed ({self.frames})")
        self.make_gif()

    def make_gif(self) -> None:
        magick = shutil.which("magick")
        frames = sorted(self.frames.glob("[0-9]*.*")) if self.frames.is_dir() else []
        if not frames:
            self.say("no GIF: no screenshots were saved")
            return
        if not magick:
            self.say("no GIF: ImageMagick's `magick` is not installed (brew install imagemagick); the frames are kept")
            return
        res = subprocess.run([magick, "-delay", "50", "-loop", "0", *map(str, frames), "-resize", "960x>",
                              str(self.gif)], capture_output=True, text=True, timeout=300, check=False)
        self.gif_made = res.returncode == 0 and self.gif.is_file()
        self.say(f"GIF: {self.gif}" if self.gif_made else f"no GIF: magick exited {res.returncode}")

    def recorded(self) -> bool:
        """The recording the release check asks for: screenshots during the run and a GIF."""
        return self.saved >= 2 and self.gif_made


class Cua:
    """The `cua` CLI on this Mac (the maintainer's signed-in account)."""

    def __init__(self, binary: str, log: Callable[[str], None], hidden: list[str]):
        self.binary, self.log, self.hidden = binary, log, hidden

    def run(self, args: list[str], timeout: float = 120, record: bool = True) -> tuple[int, str]:
        try:
            res = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout, check=False,
                                 env=dict(os.environ, DO_NOT_TRACK="1", CUA_TELEMETRY="0"), stdin=subprocess.DEVNULL)
            rc, out = res.returncode, res.stdout + res.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, out = 124, f"cua did not finish: {e}"
        self.log(f"$ cua {' '.join(args)}  (exit {rc})\n{out if record else '(output not kept: it holds the code)'}")
        return rc, out

    def pair(self, name: str) -> tuple[str, str]:
        """(code id, one-time code). The code joins `hidden` before anything can print it."""
        rc, out = self.run(["host", "pair", "--json", "--name", name, "--expires", "15"], record=False)
        try:
            data = json.loads(out[out.index("{"):])
        except ValueError:
            data = {}
        code = str(data.get("code") or "")
        if code:
            self.hidden.append(code)
        if rc != 0 or not code:
            raise RuntimeError(f"`cua host pair` gave no code (exit {rc})")
        return str(data.get("id") or "?"), code

    def spaces(self) -> list[dict]:
        rc, out = self.run(["spaces", "ls", "--json"])
        try:
            spaces = json.loads(out[out.index("{"):]).get("spaces")
        except ValueError:
            spaces = None
        if rc != 0 or not isinstance(spaces, list):
            raise RuntimeError(f"`cua spaces ls` failed (exit {rc})")
        return [s for s in spaces if isinstance(s, dict)]


class RelayProof:
    def __init__(self, orgo: Orgo, cua: Cua, res: Results, sleep: Callable[[float], None],
                 clock: Callable[[], float]):
        self.orgo, self.cua, self.res, self.sleep, self.clock = orgo, cua, res, sleep, clock
        self.cid: Optional[str] = None
        self.cli: Optional[orgo_cua.Cli] = None

    def others(self, ws: Optional[str]) -> int:
        return agent_test.computers_outside(self.orgo, ws)

    def listed(self) -> Optional[str]:
        """The relay id `cua spaces ls` shows for the computer, or None."""
        return next((str(s.get("id")) for s in self.cua.spaces() if s.get("name") == RELAY_NAME), None)

    def wait_listed(self, want: bool) -> Optional[str]:
        deadline = self.clock() + LIST_WAIT_S
        while True:
            space = self.listed()
            if bool(space) == want or self.clock() >= deadline:
                return space
            self.sleep(5)

    def run(self) -> bool:
        before = self.others(None)
        ws = self.orgo.call("POST", "/workspaces", {"name": f"cua-morning-relay-{time.strftime('%Y%m%d-%H%M%S')}"})["id"]
        self.res.say(f"relay: scratch workspace {ws}")
        passed = False
        try:
            passed = self.steps(ws)
        except (ApiError, TransportError, RuntimeError) as e:
            self.res.say(f"relay stopped: {e}")
        finally:
            cleaned = self.cleanup(ws)
            try:
                self.res.say(f"relay: computers in other workspaces: before {before}, after {self.others(ws)}")
            except (ApiError, TransportError) as e:
                self.res.say(f"relay: could not recount other workspaces: {e}")
        return passed and cleaned

    def steps(self, ws: str) -> bool:
        script = orgo_cua.SCRIPT_PATH.read_text()
        buf = io.StringIO()
        self.cli = orgo_cua.Cli(self.orgo, ws, orgo_cua.State(self.res.out_dir / "state.json"), buf, buf,
                                self.sleep, self.clock)
        code_id, code = self.cua.pair(RELAY_NAME)
        self.res.say(f"relay: pairing request {code_id} from `cua host pair` on this Mac (the code is never printed)")
        t = self.clock()
        rc = self.cli.up(RELAY_NAME, 4, 1, code, script)
        self.res.log(f"$ orgo-cua up --name {RELAY_NAME} --pair\n{buf.getvalue()}")
        entry = self.cli.state.get(self.cli.key(RELAY_NAME))
        self.cid = entry["id"] if entry else None
        paired = rc == 0 and "paired with your Cua account" in buf.getvalue()
        self.res.say(f"relay: orgo-cua up --pair: exit {rc} after {self.clock() - t:.0f} s"
                     f"{'' if paired else ' (not paired)'}")
        if not (paired and self.cid):
            return False

        space = self.wait_listed(True)
        self.res.say(f"relay: `cua spaces ls` on this Mac lists it: {space[:14] + '...' if space else 'no'}")
        if not space:
            return False

        shown = str(self.orgo.bash(self.cid, BUTTON_COMMAND, 60).get("output") or "")
        self.res.log(f"$ (button on the Orgo desktop)\n{shown}")
        button = re.search(r"^BUTTON (\d+) (\d+)$", shown, re.M)
        screen = re.search(r"^SCREEN (\d+) (\d+)$", shown, re.M)
        if not button:
            raise RuntimeError("could not put a button on the Orgo desktop")
        bx, by = int(button.group(1)), int(button.group(2))
        sw, sh = (int(screen.group(1)), int(screen.group(2))) if screen else SCREEN

        shot = self.res.out_dir / "relay-cua-screenshot.png"
        shot.unlink(missing_ok=True)
        switched = self.cua.run(["do", "switch", space])[0] == 0
        size = png_size(shot) if switched and self.cua.run(["do", "screenshot", "--save", str(shot)])[0] == 0 else None
        self.res.say(f"relay: screenshot through Cua from this Mac: {f'{size[0]}x{size[1]} ({shot})' if size else 'failed'}")
        if not size:
            return False
        x, y = round(bx * size[0] / sw), round(by * size[1] / sh)
        clicked = self.cua.run(["do", "click", str(x), str(y)])[0] == 0
        check = str(self.orgo.bash(self.cid, CHECK_COMMAND, 30).get("output") or "")
        self.res.log(f"$ (check the button)\n{check}")
        pressed = clicked and re.search(r"^RC 7$", check, re.M) is not None
        self.res.say(f"relay: click through Cua at ({x}, {y}) for the button at ({bx}, {by}) on the Orgo desktop: "
                     f"{'pressed (xmessage exit 7)' if pressed else 'did not land'}; {check.strip().splitlines()[-1:]}")
        try:
            (self.res.out_dir / "relay-orgo-after-click.png").write_bytes(self.orgo.screenshot(self.cid))
        except (ApiError, TransportError) as e:
            self.res.log(f"Orgo screenshot failed: {e}")
        if not pressed:
            return False

        buf.truncate(0)
        buf.seek(0)
        rc = self.cli.unpair(RELAY_NAME, script)
        self.res.log(f"$ orgo-cua unpair --name {RELAY_NAME}\n{buf.getvalue()}")
        gone = self.wait_listed(False) is None
        self.res.say(f"relay: orgo-cua unpair: exit {rc}; `cua spaces ls` still lists it: {'no' if gone else 'YES'}")
        return rc == 0 and gone

    def cleanup(self, ws: str) -> bool:
        if self.cli:
            buf = io.StringIO()
            self.cli.out = self.cli.err = buf
            try:
                rc = self.cli.down(RELAY_NAME)  # takes it out of the Cua account first if still paired
                self.res.say(f"relay: down: exit {rc}")
            except (ApiError, TransportError) as e:
                self.res.say(f"relay: down failed: {e}")
            self.res.log(f"$ orgo-cua down --name {RELAY_NAME}\n{buf.getvalue()}")
            if "relay-unregister" in buf.getvalue():
                self.res.say(f"relay: it may still be in your Cua account: {orgo_cua.RELAY_CLEANUP_HINT}")
        left = -1
        for _ in range(36):
            try:
                desktops = self.orgo.call("GET", f"/workspaces/{ws}").get("desktops") or []
                left = sum(1 for d in desktops if d.get("status") != "deleted")
            except (ApiError, TransportError) as e:
                self.res.log(f"workspace read: {e}")
            if left == 0:
                break
            self.sleep(5)
        self.res.say(f"relay: computers left in the scratch workspace: {left}")
        if left != 0:
            self.res.say("relay: kept the scratch workspace: it still has computers; delete them in Orgo, then the workspace.")
            return False
        try:
            self.orgo.call("POST", f"/workspaces/{ws}/delete")
        except (ApiError, TransportError) as e:
            self.res.say(f"relay: delete scratch workspace failed: {e}")
            return False
        self.res.say("relay: deleted the scratch workspace")
        return True


def main(argv: Optional[list[str]] = None, env: Optional[dict] = None, stdin: TextIO = sys.stdin,
         http: orgo_cua.Http = orgo_cua.urllib_http, encrypt: agent_test.Encrypt = agent_test.openssl_encrypt,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
         out: TextIO = sys.stdout, err: TextIO = sys.stderr, frame_s: float = FRAME_S) -> int:
    env = dict(os.environ if env is None else env)
    p = argparse.ArgumentParser(prog="morning-proof", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.parse_args(sys.argv[1:] if argv is None else argv)
    base = env.get("AGENT_TEST_ORGO_BASE") or agent_test.STAGING
    if base != agent_test.STAGING and not agent_test.LOOPBACK.match(base):
        print("Staging only: AGENT_TEST_ORGO_BASE may only name a local stand-in (http://127.0.0.1:PORT/...).", file=err)
        return 2
    model = env.get("AGENT_TEST_MODEL") or None
    if model and not agent_test.MODEL_OK.fullmatch(model):
        print("AGENT_TEST_MODEL may hold only letters, digits and . _ : -", file=err)
        return 2
    lines = [line.strip() for line in stdin.read().splitlines()]
    if len(lines) < 2 or not lines[0] or not lines[1]:
        print("Expected two lines on stdin: the Orgo staging key, then the Anthropic key.", file=err)
        return 2
    out_dir = agent_test.results_dir(env) / "morning"
    hidden = [lines[0], lines[1]]
    res = Results(out_dir, out, hidden)
    for sig in (signal.SIGTERM, signal.SIGHUP):  # a closed terminal or a kill still cleans up
        signal.signal(sig, lambda *_: sys.exit(1))

    res.say("part 1: a Cua agent drives an Orgo desktop")
    agent = FramedAgentTest(base, http, lines[0], lines[1], out_dir / "agent-run", encrypt, sleep, clock, out, model,
                            frame_s=frame_s)
    try:
        run_ok = agent.run()
    except (ApiError, TransportError) as e:
        agent.say(f"Orgo API error before anything was made: {e}")
        run_ok = False
    agent_ok = run_ok and agent.recorded()
    res.log((out_dir / "agent-run" / "log.txt").read_text())
    res.say(f"part 1: {'PASS' if agent_ok else 'FAIL'}; agent run {'passed' if run_ok else 'failed'}; screenshots "
            f"{agent.saved} saved, {agent.failed} failed; GIF {agent.gif if agent.gif_made else 'not made'} "
            f"(details: {out_dir / 'agent-run' / 'result.txt'})")

    relay = env.get("ORGO_CUA_TEST_RELAY") or "run"
    binary = env.get("ORGO_CUA_TEST_CUA") or ""
    res.say("part 2: an Orgo computer joins your Cua account (relay)")
    if relay != "run" or not binary:
        res.say(f"part 2: not run ({relay if relay != 'run' else 'no cua CLI given'})")
        relay_ok = False
    else:
        cua = Cua(binary, res.log, hidden)
        try:
            relay_ok = RelayProof(Orgo(base, lines[0], http), cua, res, sleep, clock).run()
        except (ApiError, TransportError) as e:
            res.say(f"relay: Orgo API error before anything was made: {e}")
            relay_ok = False
        res.say(f"part 2: {'PASS' if relay_ok else 'FAIL'}")

    ok = agent_ok and relay_ok
    res.say("RESULT: PASS" if ok else f"RESULT: FAIL (see {res.logfile})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
