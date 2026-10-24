"""orgo-cua: make an Orgo cloud computer usable from Cua.

  orgo-cua up     [--name NAME] [--ram GB] [--cpu N] [--pair]
  orgo-cua status [--name NAME]
  orgo-cua unpair [--name NAME]
  orgo-cua down   [--name NAME]

`up` creates the computer (or reuses the one with that name), sends
guest/setup-cua-host.sh through Orgo's bash endpoint and reports what the
computer says back. `down` deletes only a computer `up` created, by its
exact id. Python 3 standard library only.

`up --pair` joins the computer to your Cua account. The one-time code is
read from ORGO_CUA_PAIR_CODE, else asked for without echo (or read from
stdin). It never goes on a command line or into a bash request: it is
uploaded as a file to the computer, which reads and deletes it.

`unpair` takes the computer out of your Cua account (Cua's own `cua host
remove`, run inside the computer) and leaves Cua's computer service running
local-only. `down` does the same first when the computer is paired, so a
deleted computer never lingers in your Cua account.

Environment (read from the environment only, never from files):
  ORGO_API_KEY        Orgo API key (required)
  ORGO_WORKSPACE_ID   workspace to use (required)
  ORGO_BASE_URL       default https://www.orgo.ai/api
  ORGO_CUA_STATE      where `up` records the computers it created
                      (default ~/.config/orgo-cua/state.json)
  ORGO_CUA_PAIR_CODE  the pairing code for `up --pair` (optional)

Exit codes: 0 ready, 1 not ready, 2 usage or configuration, 3 Orgo API error.
"""

from __future__ import annotations

import argparse
import base64
import functools
import getpass
import json
import os
import secrets
import re
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

DEFAULT_BASE_URL = "https://www.orgo.ai/api"
DEFAULT_NAME = "cua-host"
GUEST_DIR = "/opt/orgo-cua"
GUEST_SCRIPT = f"{GUEST_DIR}/setup-cua-host.sh"
PAIR_DIR = "/root/.orgo-cua-pair"
STATUS_PREFIX = "ORGO_CUA_STATUS "
SCRIPT_PATH = Path(__file__).resolve().parent / "guest" / "setup-cua-host.sh"
USER_AGENT = "orgo-cua/0.1"
READY_TIMEOUT_S = 300
SETUP_TIMEOUT_S = 300  # Orgo clamps bash timeouts to 300 s
SECRET_LIKE = re.compile(r"[0-9A-Fa-f]{20,}|cpc_\S+|sk_\S+")


class TransportError(Exception):
    """The request never got an HTTP answer (network, TLS, timeout)."""


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(f"Orgo API {status}: {message}" + (f" ({code})" if code else ""))
        self.status = status
        self.code = code


Http = Callable[[str, str, dict, Optional[bytes], float], tuple[int, bytes]]


SYSTEM_CA_BUNDLE = "/etc/ssl/cert.pem"


@functools.cache
def tls_context() -> ssl.SSLContext:
    """Verified TLS. A python.org build on macOS starts with an empty trust store
    until its "Install Certificates" step runs; then trust the system bundle."""
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats()["x509_ca"] and os.path.isfile(SYSTEM_CA_BUNDLE):
        ctx.load_verify_locations(cafile=SYSTEM_CA_BUNDLE)
    return ctx


class KeyKeepingRedirects(urllib.request.HTTPRedirectHandler):
    """urllib copies every header, the API key too, onto a redirect to any host. Here the
    key goes along only to the same scheme and host, and https never redirects to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old, nxt = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(new.full_url)
            if old.scheme == "https" and nxt.scheme != "https":
                raise urllib.error.HTTPError(newurl, code, "refused a redirect from https to http", headers, fp)
            if (old.scheme, old.netloc) != (nxt.scheme, nxt.netloc):
                new.remove_header("Authorization")
        return new


@functools.cache
def opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()), KeyKeepingRedirects)


def urllib_http(method: str, url: str, headers: dict, body: Optional[bytes], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener().open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TransportError(str(getattr(e, "reason", e))) from None


class Orgo:
    def __init__(self, base_url: str, api_key: str, http: Http):
        self.base = base_url.rstrip("/")
        self._key = api_key
        self._http = http

    def call(self, method: str, path: str, body: Optional[dict] = None, timeout: float = 60) -> Any:
        if body is None:
            return self._send(method, path, None, None, timeout)
        return self._send(method, path, "application/json", json.dumps(body).encode(), timeout)

    def _send(self, method: str, path: str, ctype: Optional[str], data: Optional[bytes], timeout: float) -> Any:
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if ctype:
            headers["Content-Type"] = ctype
        status, raw = self._http(method, self.base + path, headers, data, timeout)
        try:
            parsed = json.loads(raw.decode() or "null")
        except (UnicodeDecodeError, ValueError):
            parsed = None
        if not 200 <= status < 300:
            msg = parsed.get("error") if isinstance(parsed, dict) else None
            code = parsed.get("code") if isinstance(parsed, dict) else None
            raise ApiError(status, str(msg or "request failed"), str(code or ""))
        return parsed

    def workspace(self, ws: str) -> dict:
        return self.call("GET", f"/workspaces/{ws}")

    def computer(self, cid: str) -> dict:
        return self.call("GET", f"/computers/{cid}")

    def create(self, ws: str, name: str, ram: int, cpu: float) -> dict:
        body = {"workspace_id": ws, "name": name, "os": "linux", "ram": ram, "cpu": cpu}
        return self.call("POST", "/computers", body, timeout=600)

    def start(self, cid: str) -> dict:
        return self.call("POST", f"/computers/{cid}/start", {}, timeout=900)

    def delete(self, cid: str) -> dict:
        return self.call("DELETE", f"/computers/{cid}", timeout=120)

    def bash(self, cid: str, command: str, timeout: int) -> dict:
        return self.call("POST", f"/computers/{cid}/bash", {"command": command, "timeout": timeout}, timeout=timeout + 45)

    def screenshot(self, cid: str) -> bytes:
        """The computer's screen as image bytes. GET /computers/{id}/screenshot answers with a
        link to the stored image; the key goes along only when the link is on the API's own host."""
        link = str((self.call("GET", f"/computers/{cid}/screenshot", timeout=60) or {}).get("image") or "")
        if link.startswith("data:"):
            return base64.b64decode(link.split(",", 1)[1])
        api = urllib.parse.urlsplit(self.base)
        if link.startswith("/"):
            link = f"{api.scheme}://{api.netloc}{link}"
        url = urllib.parse.urlsplit(link)
        if url.scheme not in ("https", "http") or (url.scheme == "http" and (url.scheme, url.netloc) != (api.scheme, api.netloc)):
            raise ApiError(502, "the screenshot answer had no usable image link")
        headers = {"User-Agent": USER_AGENT}
        if (url.scheme, url.netloc) == (api.scheme, api.netloc):
            headers["Authorization"] = f"Bearer {self._key}"
        status, raw = self._http("GET", link, headers, None, 60)
        if status != 200 or not raw:
            raise ApiError(status, "could not download the screenshot")
        return raw

    def place_file(self, ws: str, cid: str, folder: str, filename: str, content: bytes) -> dict:
        """POST /files/upload with destPath: one file into one folder on one computer, nothing recorded."""
        boundary = "orgo-cua-" + secrets.token_hex(12)
        parts = []
        for field, value in (("workspaceId", ws), ("desktopId", cid), ("destPath", folder)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._send("POST", "/files/upload", f"multipart/form-data; boundary={boundary}", b"".join(parts), 180)


class State:
    """Which computers `up` created: {"<base>|<workspace>|<name>": {"id", "created"}}."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> Optional[dict]:
        entry = self._load().get(key)
        return entry if isinstance(entry, dict) and entry.get("id") else None

    def put(self, key: str, entry: Optional[dict]) -> None:
        data = self._load()
        if entry is None:
            data.pop(key, None)
        else:
            data[key] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)


def setup_command(script: str, args: list[str], name: str) -> str:
    """The bash command that writes the guest script and runs it."""
    b64 = base64.b64encode(script.encode()).decode()
    run = " ".join([f"ORGO_CUA_NAME={shlex.quote(name)}", GUEST_SCRIPT, *map(shlex.quote, args)])
    return (
        f"mkdir -p {GUEST_DIR} && printf %s '{b64}' | base64 -d > {GUEST_SCRIPT}.new"
        f" && chmod 0755 {GUEST_SCRIPT}.new && mv -f {GUEST_SCRIPT}.new {GUEST_SCRIPT} && {run}"
    )


STATUS_COMMAND = (
    f"if [ -x {GUEST_SCRIPT} ]; then {GUEST_SCRIPT} --status; "
    f"""else echo '{STATUS_PREFIX}{{"ok": false, "error": "not set up (run orgo-cua up)"}}'; fi"""
)
AGENT_DEPS_COMMAND = f"{GUEST_SCRIPT} --agent-deps"
RELAY_CLEANUP_HINT = ("remove it from your Cua account on a signed-in device: `cua spaces ls` shows it "
                      "as relay:<id>, then `cua spaces relay-unregister relay:<id>`")


def parse_status(output: str) -> Optional[dict]:
    for line in reversed((output or "").splitlines()):
        if line.startswith(STATUS_PREFIX):
            try:
                value = json.loads(line[len(STATUS_PREFIX):])
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def redact(text: str) -> str:
    return SECRET_LIKE.sub("<redacted>", text)


class Cli:
    def __init__(self, orgo: Orgo, ws: str, state: State, out: TextIO, err: TextIO, sleep: Callable[[float], None], clock: Callable[[], float]):
        self.orgo, self.ws, self.state = orgo, ws, state
        self.out, self.err, self.sleep, self.clock = out, err, sleep, clock

    def say(self, msg: str) -> None:
        print(msg, file=self.out)

    def key(self, name: str) -> str:
        return f"{self.orgo.base}|{self.ws}|{name}"

    def by_name(self, name: str) -> Optional[dict]:
        for d in self.orgo.workspace(self.ws).get("desktops") or []:
            if isinstance(d, dict) and d.get("name") == name:
                return d
        return None

    def find(self, name: str) -> tuple[Optional[dict], Optional[dict]]:
        """(computer, state entry): the recorded id first, else the exact name."""
        entry = self.state.get(self.key(name))
        if entry:
            try:
                return self.orgo.computer(entry["id"]), entry
            except ApiError as e:
                if e.status != 404:
                    raise
                self.state.put(self.key(name), None)
                entry = None
        return self.by_name(name), entry

    def create(self, name: str, ram: int, cpu: float) -> dict:
        self.say(f"Creating Orgo computer {name!r} ({ram} GB, {cpu:g} vCPU)...")
        try:
            comp = self.orgo.create(self.ws, name, ram, cpu)
            created = True
        except (TransportError, ApiError) as e:
            if isinstance(e, ApiError) and e.status == 409 and e.code.lower() == "name_taken":
                comp, created = self.by_name(name), False
            elif isinstance(e, TransportError) or e.status >= 500:
                # The create may have gone through even though the answer was lost.
                self.sleep(5)
                comp, created = self.by_name(name), True
            else:
                raise
            if comp is None:
                raise
        self.state.put(self.key(name), {"id": comp["id"], "created": created})
        return comp

    def wait_ready(self, cid: str) -> None:
        deadline = self.clock() + READY_TIMEOUT_S
        last = ""
        while True:
            try:
                status = self.orgo.computer(cid).get("status")
                if status == "running":
                    if self.orgo.bash(cid, "true", 10).get("exit_code") == 0:
                        return
                last = f"status {status}"
            except TransportError as e:
                last = f"no answer ({e})"
            except ApiError as e:
                if e.status not in (400, 409, 502, 503, 504):
                    raise
                last = str(e)
            if self.clock() >= deadline:
                raise ApiError(503, f"computer not ready after {READY_TIMEOUT_S} s ({last})")
            self.sleep(5)

    def run_guest(self, cid: str, command: str) -> Optional[dict]:
        res = self.orgo.bash(cid, command, SETUP_TIMEOUT_S)
        output = str(res.get("output") or "")
        status = parse_status(output)
        if status is None:
            print(f"The computer gave no status line (exit {res.get('exit_code')}). Last output:", file=self.err)
            for line in output.splitlines()[-15:]:
                print("  " + redact(line), file=self.err)
        return status

    def setup(self, cid: str, command: str) -> Optional[dict]:
        status = self.run_guest(cid, command)
        # Cua's agent installs need xz. Installing it is its own call, so apt
        # never shares setup's 300 s (see ensure_xz in the guest script).
        if status and status.get("ok") and "xz" in (status.get("missing") or []):
            self.say("Installing xz-utils (Cua's agent installs unpack Node with it)...")
            status = self.run_guest(cid, AGENT_DEPS_COMMAND) or status
        return status

    def report(self, comp: dict, status: Optional[dict]) -> int:
        if status is None:
            return 1
        self.say("status: " + json.dumps(status))
        name, cid = comp.get("name"), comp.get("id")
        if not status.get("ok"):
            self.say(f"Not ready: {status.get('error') or 'see status above'}")
            return 1
        if status.get("mode") == "relay":
            self.say(f"Ready: {name!r} ({cid}) is paired with your Cua account and shows in your Cua Spaces list.")
            return 0
        self.say(
            f"Ready: Cua's computer service runs on the Orgo desktop of {name!r} ({cid}), "
            f"listening on {status.get('listen')} inside the computer only."
        )
        self.say(
            "Next step: pair it with your Cua account. In the Cua Spaces app choose Add a computer "
            "(or run `cua host pair` on a signed-in device) to get a one-time code, then run this "
            "and paste the code when asked:\n"
            f"  orgo-cua up --name {shlex.quote(str(name))} --pair"
        )
        return 0

    def up(self, name: str, ram: int, cpu: float, pair_code: Optional[str], script: str) -> int:
        comp, entry = self.find(name)
        if comp is None:
            comp = self.create(name, ram, cpu)
        elif entry is None:
            self.state.put(self.key(name), {"id": comp["id"], "created": False})
            self.say(f"Reusing existing computer {name!r} ({comp['id']}).")
        else:
            self.say(f"Reusing computer {name!r} ({comp['id']}).")
        cid = comp["id"]
        status = comp.get("status")
        if status in ("frozen", "stopped", "error"):
            self.say(f"Starting it (was {status})...")
            self.orgo.start(cid)
        elif status == "deleted":
            raise ApiError(410, f"computer {cid} is deleted")
        self.wait_ready(cid)
        self.say("Setting up Cua inside the computer (pinned Cua CLI and cua-spacesd)...")
        if not pair_code:
            return self.report(comp, self.setup(cid, setup_command(script, [], name)))
        # The code travels as a file, never in a command: the guest reads and deletes it.
        filename = f"pair-{secrets.token_hex(8)}.code"
        self.orgo.place_file(self.ws, cid, PAIR_DIR, filename, pair_code.encode())
        path = f"{PAIR_DIR}/{filename}"
        try:
            return self.report(comp, self.setup(cid, setup_command(script, ["--pair-file", path], name)))
        finally:
            try:
                self.orgo.bash(cid, f"rm -f {path}", 30)
            except (ApiError, TransportError):
                print(f"Could not confirm {path} was removed from the computer; delete it there.", file=self.err)

    def leave_cua(self, comp: dict, name: str, script: str) -> tuple[bool, bool]:
        """Takes a paired computer out of the user's Cua account. Returns (out of the account,
        Cua's service healthy afterwards); a computer that was never paired counts as out."""
        status = self.run_guest(comp["id"], STATUS_COMMAND)
        if not status or status.get("mode") != "relay":
            if status:
                self.say(f"{name!r} is not paired with a Cua account.")
            return status is not None, bool(status and status.get("ok"))
        self.say(f"Taking {name!r} out of your Cua account...")
        after = self.run_guest(comp["id"], setup_command(script, ["--unpair"], name)) or {}
        left = after.get("mode") != "relay" and "unpaired" in (after.get("changed") or [])
        if left and after.get("ok"):
            self.say("Removed from your Cua account; Cua's computer service is back to local-only.")
        elif left:
            self.say(f"Removed from your Cua account, but Cua's computer service did not come back: "
                     f"{after.get('error') or 'see the status above'}. Run `orgo-cua up` to repair it.")
        else:
            self.say(f"Could not take {name!r} out of your Cua account: "
                     f"{after.get('error') or 'no status from the computer'}.")
        return left, left and bool(after.get("ok"))

    def unpair(self, name: str, script: str) -> int:
        comp, _ = self.find(name)
        if comp is None:
            self.say(f"No computer named {name!r} in this workspace.")
            return 1
        if comp.get("status") != "running":
            self.say(f"Computer {name!r} is {comp.get('status')}; start it (orgo-cua up) to unpair it.")
            return 1
        left, healthy = self.leave_cua(comp, name, script)
        return 0 if left and healthy else 1

    def status(self, name: str) -> int:
        comp, _ = self.find(name)
        if comp is None:
            self.say(f"No computer named {name!r} in this workspace.")
            return 1
        self.say(f"Computer {name!r} ({comp['id']}): {comp.get('status')}")
        if comp.get("status") != "running":
            return 1
        return self.report(comp, self.run_guest(comp["id"], STATUS_COMMAND))

    def down(self, name: str, script: Optional[str] = None) -> int:
        key = self.key(name)
        entry = self.state.get(key)
        if entry is None or not entry.get("created"):
            existing = self.by_name(name)
            if existing is None:
                self.say(f"Nothing to delete: no computer named {name!r}.")
                return 0
            self.say(
                f"Not deleting {name!r} ({existing['id']}): orgo-cua did not create it. "
                "Delete it in Orgo yourself if you mean to."
            )
            return 1
        cid = entry["id"]
        try:
            comp = self.orgo.computer(cid)
        except ApiError as e:
            if e.status != 404:
                raise
            self.state.put(key, None)
            self.say(f"Already gone: {cid}.")
            return 0
        if comp.get("name") != name:
            self.say(f"Not deleting {cid}: it is now named {comp.get('name')!r}, not {name!r}.")
            return 1
        left = False
        if comp.get("status") == "running":
            try:
                left, _ = self.leave_cua(comp, name, script or SCRIPT_PATH.read_text())
            except (ApiError, TransportError) as e:
                self.say(f"Could not check its Cua pairing: {redact(str(e))}")
        if not left:
            self.say(f"If {name!r} was paired with Cua, {RELAY_CLEANUP_HINT}.")
        self.orgo.delete(cid)
        self.state.put(key, None)
        self.say(f"Deleted {name!r} ({cid}).")
        return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="orgo-cua", description="Make an Orgo computer usable from Cua.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd, help_ in (("up", "create or reuse the computer and set Cua up on it"),
                       ("status", "show whether Cua is ready on the computer"),
                       ("unpair", "take the computer out of your Cua account"),
                       ("down", "delete the computer orgo-cua created")):
        s = sub.add_parser(cmd, help=help_)
        s.add_argument("--name", default=DEFAULT_NAME, help=f"computer name (default {DEFAULT_NAME})")
        if cmd == "up":
            s.add_argument("--ram", type=int, default=4, choices=[4, 8, 16, 32, 64], help="GB (default 4)")
            s.add_argument("--cpu", type=float, default=1, choices=[0.5, 1, 2, 4], help="vCPU (default 1)")
            s.add_argument("--pair", action="store_true",
                           help="join your Cua account with a one-time code (asked for, never an argument)")
    return p.parse_args(argv)


def read_pair_code(env: dict, stdin: TextIO, err: TextIO) -> str:
    code = (env.get("ORGO_CUA_PAIR_CODE") or "").strip()
    if code:
        return code
    if stdin.isatty():
        return getpass.getpass("Cua pairing code (input hidden): ", stream=err).strip()
    return stdin.readline().strip()


def main(argv: Optional[list[str]] = None, env: Optional[dict] = None, http: Http = urllib_http,
         out: TextIO = sys.stdout, err: TextIO = sys.stderr, sleep: Callable[[float], None] = time.sleep,
         clock: Callable[[], float] = time.monotonic, script_path: Path = SCRIPT_PATH,
         stdin: TextIO = sys.stdin) -> int:
    env = dict(os.environ if env is None else env)
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as e:
        return 2 if e.code else 0
    key = (env.get("ORGO_API_KEY") or "").strip()
    ws = (env.get("ORGO_WORKSPACE_ID") or "").strip()
    base = (env.get("ORGO_BASE_URL") or DEFAULT_BASE_URL).strip()
    if not key or not ws:
        print("Set ORGO_API_KEY and ORGO_WORKSPACE_ID in the environment.", file=err)
        return 2
    if not re.match(r"https://|http://(127\.0\.0\.1|localhost)[:/]", base):
        print("ORGO_BASE_URL must be https:// (the API key would travel in plain text).", file=err)
        return 2
    state_path = Path(env.get("ORGO_CUA_STATE") or Path(env.get("HOME", "~")).expanduser() / ".config/orgo-cua/state.json")
    pair_code = None
    if args.cmd == "up" and args.pair:
        pair_code = read_pair_code(env, stdin, err)
        if not pair_code:
            print("No pairing code given.", file=err)
            return 2
    cli = Cli(Orgo(base, key, http), ws, State(state_path), out, err, sleep, clock)
    try:
        if args.cmd == "up":
            return cli.up(args.name, args.ram, args.cpu, pair_code, script_path.read_text())
        if args.cmd == "status":
            return cli.status(args.name)
        if args.cmd == "unpair":
            return cli.unpair(args.name, script_path.read_text())
        return cli.down(args.name, script_path.read_text())
    except ApiError as e:
        print(redact(str(e)), file=err)
        return 3
    except TransportError as e:
        print(f"Could not reach Orgo: {redact(str(e))}", file=err)
        return 3


if __name__ == "__main__":
    sys.exit(main())
