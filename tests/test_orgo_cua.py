"""Unit tests for orgo-cua with a fake Orgo API (no network)."""

import base64
import io
import json
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orgo_cua  # noqa: E402

KEY = "sk_live_test_key_0123456789abcdef"
PAIR = "K7QM-4XPB-2RHD"
WS = "ws-1"
BASE = "https://orgo.test/api"
OK_STATUS = {
    "ok": True, "cua": "0.5.0", "spacesd": "0.5.3", "mode": "direct", "listen": "127.0.0.1:3211",
    "service": "RUNNING", "caps": "ok", "missing": ["h264_hw"], "changed": ["host"], "error": "",
}


class FakeOrgo:
    """In-memory Orgo API. `bash_hook(cid, command)` returns the bash output."""

    def __init__(self):
        self.desktops = {}
        self.calls = []
        self.fail_next = {}  # (method, path regex) -> list of exceptions or (status, body)
        self.bash_hook = lambda cid, command: orgo_cua.STATUS_PREFIX + json.dumps(OK_STATUS)
        self.seen_auth = set()
        self.bash_commands = []
        self.uploads = []  # raw multipart bodies

    def add(self, name, status="running"):
        cid = str(uuid.uuid4())
        self.desktops[cid] = {"id": cid, "name": name, "status": status}
        return cid

    def __call__(self, method, url, headers, body, timeout):
        assert url.startswith(BASE), url
        path = url[len(BASE):]
        self.calls.append((method, path))
        self.seen_auth.add(headers.get("Authorization"))
        for (m, rx), queue in self.fail_next.items():
            if m == method and re.fullmatch(rx, path) and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item[0], json.dumps(item[1]).encode()
        if (headers.get("Content-Type") or "").startswith("multipart/form-data; boundary="):
            status, payload = self.upload(path, body)
            return status, json.dumps(payload).encode()
        data = json.loads(body) if body else None
        if method == "POST" and path.endswith("/bash"):
            self.bash_commands.append(data["command"])
        status, payload = self.route(method, path, data)
        return status, json.dumps(payload).encode()

    def upload(self, path, body):
        assert path == "/files/upload", path
        self.uploads.append(body)
        text = body.decode()
        fields = dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', text))
        if fields.get("desktopId") not in self.desktops:
            return 503, {"error": "Computer could not be reached."}
        return 200, {"ok": True, "placedIn": fields["destPath"]}

    def route(self, method, path, data):
        if method == "GET" and path == f"/workspaces/{WS}":
            return 200, {"id": WS, "desktops": list(self.desktops.values())}
        if method == "POST" and path == "/computers":
            if any(d["name"] == data["name"] for d in self.desktops.values()):
                return 409, {"error": "name taken", "code": "NAME_TAKEN"}
            cid = self.add(data["name"])
            self.desktops[cid].update(ram=data["ram"], cpu=data["cpu"], os=data["os"])
            return 201, self.desktops[cid]
        m = re.fullmatch(r"/computers/([^/]+)(/\w+)?", path)
        if not m:
            return 404, {"error": "no route"}
        cid, action = m.group(1), m.group(2)
        comp = self.desktops.get(cid)
        if comp is None:
            return 404, {"error": "Desktop not found"}
        if method == "GET" and action is None:
            return 200, comp
        if method == "DELETE" and action is None:
            del self.desktops[cid]
            return 200, {"success": True}
        if method == "POST" and action == "/start":
            comp["status"] = "running"
            return 200, {"success": True}
        if method == "POST" and action == "/bash":
            if data["command"] == "true":
                return 200, {"success": True, "output": "", "exit_code": 0}
            return 200, {"success": True, "command": data["command"], "output": self.bash_hook(cid, data["command"]), "exit_code": 0}
        return 404, {"error": "no route"}

    def count(self, method, rx):
        return sum(1 for m, p in self.calls if m == method and re.fullmatch(rx, p))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.fake = FakeOrgo()
        self.env = {"ORGO_API_KEY": KEY, "ORGO_WORKSPACE_ID": WS, "ORGO_BASE_URL": BASE, "ORGO_CUA_STATE": str(self.state)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, env=None, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        clock = iter(range(0, 10_000, 5))
        code = orgo_cua.main(list(argv), env=env or self.env, http=self.fake, out=out, err=err,
                             sleep=lambda s: None, clock=lambda: next(clock), stdin=stdin or io.StringIO(""))
        self.out, self.err = out.getvalue(), err.getvalue()
        # The API key never reaches the output, and only travels as a Bearer header.
        self.assertNotIn(KEY, self.out + self.err)
        self.assertLessEqual(self.fake.seen_auth, {f"Bearer {KEY}"})
        # The pairing code is never printed and never sent inside a bash command.
        self.assertNotIn(PAIR, self.out + self.err)
        for command in self.fake.bash_commands:
            self.assertNotIn(PAIR, command)
        return code


class UpTests(Base):
    def test_creates_sends_the_guest_script_and_prints_the_pairing_step(self):
        commands = []
        self.fake.bash_hook = lambda cid, c: commands.append(c) or "orgo-cua: installed\n" + orgo_cua.STATUS_PREFIX + json.dumps(OK_STATUS)
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 0)
        (comp,) = self.fake.desktops.values()
        self.assertEqual((comp["name"], comp["ram"], comp["cpu"], comp["os"]), ("cua-kit", 4, 1, "linux"))
        (cmd,) = commands
        b64 = re.search(r"printf %s '([A-Za-z0-9+/=]+)' \| base64 -d", cmd).group(1)
        self.assertEqual(base64.b64decode(b64).decode(), orgo_cua.SCRIPT_PATH.read_text())
        self.assertTrue(cmd.endswith("ORGO_CUA_NAME=cua-kit /opt/orgo-cua/setup-cua-host.sh"), cmd)
        self.assertIn("cua host pair", self.out)
        self.assertIn("orgo-cua up --name cua-kit --pair", self.out)
        self.assertEqual(json.loads(self.state.read_text()), {f"{BASE}|{WS}|cua-kit": {"id": comp["id"], "created": True}})

    def test_installs_xz_in_its_own_call_when_setup_reports_it_missing(self):
        commands = []
        deps = dict(OK_STATUS, changed=["xz-utils"])

        def hook(cid, c):
            commands.append(c)
            if c == orgo_cua.AGENT_DEPS_COMMAND:
                return orgo_cua.STATUS_PREFIX + json.dumps(deps)
            return orgo_cua.STATUS_PREFIX + json.dumps(dict(OK_STATUS, missing=["h264_hw", "xz"]))
        self.fake.bash_hook = hook
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 0)
        self.assertEqual(len(commands), 2)
        self.assertTrue(commands[0].endswith("/opt/orgo-cua/setup-cua-host.sh"), commands[0])
        self.assertEqual(commands[1], "/opt/orgo-cua/setup-cua-host.sh --agent-deps")
        self.assertIn('"changed": ["xz-utils"]', self.out)  # the status shown is the second call's

    def test_a_failed_setup_never_installs_xz(self):
        commands = []
        bad = dict(OK_STATUS, ok=False, missing=["xz"], error="cua host setup failed")
        self.fake.bash_hook = lambda cid, c: commands.append(c) or orgo_cua.STATUS_PREFIX + json.dumps(bad)
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 1)
        self.assertEqual(len(commands), 1)

    def test_reuses_a_computer_with_the_same_name_and_records_it_as_not_created(self):
        cid = self.fake.add("cua-kit")
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 0)
        self.assertEqual(self.fake.count("POST", "/computers"), 0)
        self.assertIn("Reusing existing computer", self.out)
        self.assertEqual(json.loads(self.state.read_text())[f"{BASE}|{WS}|cua-kit"], {"id": cid, "created": False})

    def test_second_up_uses_the_recorded_id(self):
        self.run_cli("up", "--name", "cua-kit")
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 0)
        self.assertEqual(self.fake.count("POST", "/computers"), 1)
        self.assertEqual(json.loads(self.state.read_text())[f"{BASE}|{WS}|cua-kit"]["created"], True)

    def test_starts_a_frozen_computer_first(self):
        cid = self.fake.add("cua-kit", status="frozen")
        self.assertEqual(self.run_cli("up", "--name", "cua-kit"), 0)
        self.assertEqual(self.fake.count("POST", f"/computers/{cid}/start"), 1)

    def test_waits_until_bash_answers(self):
        self.fake.fail_next[("POST", r"/computers/[^/]+/bash")] = [
            (503, {"error": "Computer not ready"}), orgo_cua.TransportError("reset"), (503, {"error": "Could not reach the desktop"})]
        self.assertEqual(self.run_cli("up"), 0)
        self.assertEqual(self.fake.count("POST", r"/computers/[^/]+/bash"), 5)  # 3 failures, ready, setup

    def test_gives_up_when_the_computer_never_answers(self):
        self.fake.fail_next[("POST", r"/computers/[^/]+/bash")] = [(503, {"error": "Computer not ready"})] * 500
        self.assertEqual(self.run_cli("up"), 3)
        self.assertIn("not ready after", self.err)

    def test_create_whose_answer_was_lost_is_found_by_name_and_owned(self):
        def lost(method, url, headers, body, timeout):
            self.fake.route("POST", "/computers", json.loads(body))  # it was created...
            raise orgo_cua.TransportError("timed out")  # ...but the answer never came

        real = self.fake
        http = lambda m, u, h, b, t: lost(m, u, h, b, t) if (m, u) == ("POST", BASE + "/computers") else real(m, u, h, b, t)
        out, err = io.StringIO(), io.StringIO()
        code = orgo_cua.main(["up"], env=self.env, http=http, out=out, err=err, sleep=lambda s: None)
        self.assertEqual(code, 0, err.getvalue())
        (comp,) = self.fake.desktops.values()
        self.assertEqual(json.loads(self.state.read_text())[f"{BASE}|{WS}|cua-host"], {"id": comp["id"], "created": True})

    def test_no_status_line_is_not_ready_and_hides_secrets(self):
        self.fake.bash_hook = lambda cid, c: "boom\ntoken 0123456789abcdef0123456789abcdef\n"
        self.assertEqual(self.run_cli("up"), 1)
        self.assertIn("gave no status line", self.err)
        self.assertNotIn("0123456789abcdef0123456789abcdef", self.err)

    def test_guest_not_ok_exits_1(self):
        bad = dict(OK_STATUS, ok=False, caps="fail", error="not ready (service FATAL, mode direct, caps fail)")
        self.fake.bash_hook = lambda cid, c: orgo_cua.STATUS_PREFIX + json.dumps(bad)
        self.assertEqual(self.run_cli("up"), 1)
        self.assertIn("Not ready: not ready (service FATAL", self.out)
        self.assertNotIn("Next step", self.out)

    def pair_up(self, **kw):
        paired = dict(OK_STATUS, mode="relay", listen="", caps="skipped")
        self.fake.bash_hook = lambda cid, c: orgo_cua.STATUS_PREFIX + json.dumps(paired)
        return self.run_cli("up", "--pair", **kw)

    def test_pair_code_travels_only_as_an_uploaded_file(self):
        self.assertEqual(self.pair_up(env=dict(self.env, ORGO_CUA_PAIR_CODE=PAIR)), 0)
        (body,) = self.fake.uploads
        self.assertIn(f"\r\n\r\n{PAIR}\r\n".encode(), body)
        self.assertIn(b'name="destPath"\r\n\r\n/root/.orgo-cua-pair\r\n', body)
        name = re.search(rb'filename="(pair-[0-9a-f]{16}\.code)"', body).group(1).decode()
        setup, cleanup = self.fake.bash_commands[-2:]
        self.assertTrue(setup.endswith(f"setup-cua-host.sh --pair-file /root/.orgo-cua-pair/{name}"), setup)
        self.assertEqual(cleanup, f"rm -f /root/.orgo-cua-pair/{name}")
        self.assertIn("paired with your Cua account", self.out)

    def test_pair_code_from_stdin_when_not_a_terminal(self):
        self.assertEqual(self.pair_up(stdin=io.StringIO(PAIR + "\n")), 0)
        self.assertEqual(len(self.fake.uploads), 1)

    def test_pair_file_is_removed_even_when_setup_fails(self):
        self.fake.bash_hook = lambda cid, c: "crash"
        self.assertEqual(self.run_cli("up", "--pair", stdin=io.StringIO(PAIR)), 1)
        self.assertTrue(self.fake.bash_commands[-1].startswith("rm -f /root/.orgo-cua-pair/pair-"))

    def test_pair_code_as_an_argument_is_refused(self):
        self.assertEqual(self.run_cli("up", "--pair", "ABCD-EFGH-JKMN"), 2)
        self.assertEqual(self.fake.calls, [])

    def test_empty_pair_code_exits_2(self):
        self.assertEqual(self.pair_up(), 2)
        self.assertIn("No pairing code", self.err)
        self.assertEqual(self.fake.calls, [])

    def test_api_error_exits_3_with_its_code(self):
        self.fake.fail_next[("POST", "/computers")] = [(403, {"error": "Plan limit", "code": "VM_SLOT_ADDON"})]
        self.assertEqual(self.run_cli("up"), 3)
        self.assertIn("403: Plan limit (VM_SLOT_ADDON)", self.err)
        self.assertFalse(self.state.exists())


class DownTests(Base):
    def test_deletes_only_the_recorded_id(self):
        other = self.fake.add("someone-else")
        self.run_cli("up", "--name", "cua-kit")
        mine = next(c for c, d in self.fake.desktops.items() if d["name"] == "cua-kit")
        self.assertEqual(self.run_cli("down", "--name", "cua-kit"), 0)
        self.assertEqual([p for m, p in self.fake.calls if m == "DELETE"], [f"/computers/{mine}"])
        self.assertEqual(list(self.fake.desktops), [other])
        self.assertEqual(json.loads(self.state.read_text()), {})

    def test_refuses_a_computer_it_did_not_create(self):
        self.fake.add("cua-kit")
        self.run_cli("up", "--name", "cua-kit")  # reused, not created
        self.assertEqual(self.run_cli("down", "--name", "cua-kit"), 1)
        self.assertIn("did not create it", self.out)
        self.assertEqual(self.fake.count("DELETE", r".*"), 0)

    def test_refuses_a_name_match_with_no_record(self):
        self.fake.add("cua-host")
        self.assertEqual(self.run_cli("down"), 1)
        self.assertEqual(self.fake.count("DELETE", r".*"), 0)

    def test_already_deleted_clears_the_record(self):
        self.run_cli("up")
        self.fake.desktops.clear()
        self.assertEqual(self.run_cli("down"), 0)
        self.assertIn("Already gone", self.out)
        self.assertEqual(json.loads(self.state.read_text()), {})
        self.assertEqual(self.fake.count("DELETE", r".*"), 0)

    def test_refuses_when_the_recorded_id_was_renamed(self):
        self.run_cli("up")
        (comp,) = self.fake.desktops.values()
        comp["name"] = "renamed"
        self.assertEqual(self.run_cli("down"), 1)
        self.assertEqual(self.fake.count("DELETE", r".*"), 0)

    def paired_hook(self, unpair_works=True):
        relay = dict(OK_STATUS, mode="relay", listen="", caps="skipped", changed=[])
        back = (dict(OK_STATUS, changed=["unpaired", "host"]) if unpair_works else
                dict(relay, ok=False, error="could not leave the Cua account (cua host remove failed)"))
        order = []

        def hook(cid, c):
            if c == orgo_cua.STATUS_COMMAND:
                order.append("status")
                return orgo_cua.STATUS_PREFIX + json.dumps(relay)
            if c.endswith("setup-cua-host.sh --unpair"):
                order.append("unpair")
                return orgo_cua.STATUS_PREFIX + json.dumps(back)
            return orgo_cua.STATUS_PREFIX + json.dumps(OK_STATUS)
        self.fake.bash_hook = hook
        return order

    def test_a_paired_computer_leaves_the_cua_account_before_it_is_deleted(self):
        # 2026-10-24: a deleted, still-paired computer stays in the user's Cua account.
        self.run_cli("up", "--name", "cua-kit")
        order = self.paired_hook()
        real = self.fake.route
        self.fake.route = lambda m, p, d: (order.append("delete") if m == "DELETE" else None) or real(m, p, d)
        self.assertEqual(self.run_cli("down", "--name", "cua-kit"), 0)
        self.assertEqual(order, ["status", "unpair", "delete"])
        self.assertIn("Removed from your Cua account", self.out)
        self.assertNotIn("relay-unregister", self.out)
        self.assertEqual(self.fake.desktops, {})

    def test_a_failed_unpair_still_deletes_and_says_how_to_finish(self):
        self.run_cli("up", "--name", "cua-kit")
        self.paired_hook(unpair_works=False)
        self.assertEqual(self.run_cli("down", "--name", "cua-kit"), 0)
        self.assertIn("Could not take 'cua-kit' out of your Cua account", self.out)
        self.assertIn("cua spaces relay-unregister relay:<id>", self.out)
        self.assertEqual(self.fake.desktops, {})

    def test_a_local_only_computer_is_deleted_without_an_unpair(self):
        self.run_cli("up", "--name", "cua-kit")
        order = self.paired_hook()
        self.fake.bash_hook = lambda cid, c: order.append(c) or orgo_cua.STATUS_PREFIX + json.dumps(OK_STATUS)
        self.assertEqual(self.run_cli("down", "--name", "cua-kit"), 0)
        self.assertEqual(order, [orgo_cua.STATUS_COMMAND])
        self.assertNotIn("relay-unregister", self.out)


class UnpairTests(Base):
    def test_unpair_sends_the_current_guest_script_with_unpair(self):
        self.run_cli("up", "--name", "cua-kit")
        commands = []
        relay = dict(OK_STATUS, mode="relay", listen="", caps="skipped")

        def hook(cid, c):
            commands.append(c)
            back = dict(OK_STATUS, changed=["unpaired", "host"])
            return orgo_cua.STATUS_PREFIX + json.dumps(relay if c == orgo_cua.STATUS_COMMAND else back)
        self.fake.bash_hook = hook
        self.assertEqual(self.run_cli("unpair", "--name", "cua-kit"), 0)
        status, unpair = commands
        self.assertEqual(status, orgo_cua.STATUS_COMMAND)
        b64 = re.search(r"printf %s '([A-Za-z0-9+/=]+)' \| base64 -d", unpair).group(1)
        self.assertEqual(base64.b64decode(b64).decode(), orgo_cua.SCRIPT_PATH.read_text())
        self.assertTrue(unpair.endswith("ORGO_CUA_NAME=cua-kit /opt/orgo-cua/setup-cua-host.sh --unpair"), unpair)
        self.assertEqual(self.fake.count("DELETE", r".*"), 0)

    def test_unpair_whose_service_does_not_come_back_is_not_success(self):
        # Review r1 T2: out of the account, but the local service is down: exit 1, and say so.
        self.run_cli("up", "--name", "cua-kit")
        relay = dict(OK_STATUS, mode="relay", listen="", caps="skipped")
        broken = dict(OK_STATUS, ok=False, changed=["unpaired"], service="STOPPED",
                      error="not ready (service STOPPED, mode direct, caps fail)")
        self.fake.bash_hook = lambda cid, c: orgo_cua.STATUS_PREFIX + json.dumps(
            relay if c == orgo_cua.STATUS_COMMAND else broken)
        self.assertEqual(self.run_cli("unpair", "--name", "cua-kit"), 1)
        self.assertIn("Removed from your Cua account, but Cua's computer service did not come back", self.out)
        self.assertNotIn("back to local-only", self.out)

    def test_unpair_of_a_stopped_computer_asks_to_start_it(self):
        self.fake.add("cua-host", status="stopped")
        self.assertEqual(self.run_cli("unpair"), 1)
        self.assertIn("start it", self.out)
        self.assertEqual(self.fake.bash_commands, [])


class StatusAndConfigTests(Base):
    def test_status_runs_the_guest_status_check(self):
        commands = []
        self.fake.bash_hook = lambda cid, c: commands.append(c) or orgo_cua.STATUS_PREFIX + json.dumps(dict(OK_STATUS, changed=[]))
        self.run_cli("up")
        commands.clear()
        self.assertEqual(self.run_cli("status"), 0)
        self.assertEqual(commands, [orgo_cua.STATUS_COMMAND])
        self.assertIn('"caps": "ok"', self.out)

    def test_status_of_a_stopped_computer(self):
        self.fake.add("cua-host", status="frozen")
        self.assertEqual(self.run_cli("status"), 1)
        self.assertIn(": frozen", self.out)

    def test_missing_environment_exits_2(self):
        self.assertEqual(self.run_cli("up", env={"ORGO_WORKSPACE_ID": WS}), 2)
        self.assertIn("ORGO_API_KEY", self.err)
        self.assertEqual(self.fake.calls, [])

    def test_plain_http_base_url_is_refused(self):
        self.assertEqual(self.run_cli("up", env=dict(self.env, ORGO_BASE_URL="http://orgo.test/api")), 2)
        self.assertEqual(self.fake.calls, [])

    def test_parse_status_takes_the_last_status_line(self):
        out = "x\nORGO_CUA_STATUS {\"ok\": false}\nnoise\nORGO_CUA_STATUS {\"ok\": true}\ntrailing"
        self.assertEqual(orgo_cua.parse_status(out), {"ok": True})
        self.assertIsNone(orgo_cua.parse_status("ORGO_CUA_STATUS not-json"))
        self.assertIsNone(orgo_cua.parse_status(""))

    def test_setup_command_quotes_names_and_arguments(self):
        cmd = orgo_cua.setup_command("echo hi\n", ["--pair", "a b;rm"], "x'y")
        self.assertIn("ORGO_CUA_NAME='x'\"'\"'y'", cmd)
        self.assertTrue(cmd.endswith("--pair 'a b;rm'"))


class ScreenshotTests(unittest.TestCase):
    """Orgo answers GET /computers/{id}/screenshot with a link to the stored image."""

    def fetch(self, link):
        seen = []

        def http(method, url, headers, body, timeout):
            seen.append((url, headers.get("Authorization")))
            if url.endswith("/screenshot"):
                return 200, json.dumps({"success": True, "image": link}).encode()
            return 200, b"\x89PNG-bytes"

        data = orgo_cua.Orgo(BASE, KEY, http).screenshot("c1")
        return data, seen[1:]

    def test_a_link_on_the_api_host_is_fetched_with_the_key(self):
        data, seen = self.fetch("/api/storage/shot.png")
        self.assertEqual(data, b"\x89PNG-bytes")
        self.assertEqual(seen, [("https://orgo.test/api/storage/shot.png", f"Bearer {KEY}")])

    def test_the_key_never_goes_to_another_host(self):
        data, seen = self.fetch("https://objects.example/bucket/shot.png?X-Amz-Signature=abc")
        self.assertEqual(data, b"\x89PNG-bytes")
        self.assertEqual(seen, [("https://objects.example/bucket/shot.png?X-Amz-Signature=abc", None)])

    def test_inline_images_and_plain_http_elsewhere(self):
        data, seen = self.fetch("data:image/png;base64," + base64.b64encode(b"\x89PNG-inline").decode())
        self.assertEqual((data, seen), (b"\x89PNG-inline", []))
        with self.assertRaises(orgo_cua.ApiError):
            self.fetch("http://objects.example/shot.png")


class LoopbackTests(Base):
    """The real urllib transport against a local HTTP server (127.0.0.1 only)."""

    def test_up_with_pair_over_a_real_socket(self):
        import email.parser
        import http.server
        import threading

        fake, uploads = self.fake, []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_one(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or None
                ctype = self.headers.get("Content-Type") or ""
                if ctype.startswith("multipart/"):
                    msg = email.parser.BytesParser().parsebytes(f"Content-Type: {ctype}\r\n\r\n".encode() + body)
                    uploads.append({p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                                    for p in msg.get_payload()})
                url = "https://orgo.test/api" + self.path[len("/api"):]
                status, raw = fake(self.command, url, dict(self.headers), body, 5)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_DELETE = handle_one

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        paired = dict(OK_STATUS, mode="relay", listen="", caps="skipped")
        self.fake.bash_hook = lambda cid, c: orgo_cua.STATUS_PREFIX + json.dumps(paired)
        env = dict(self.env, ORGO_BASE_URL=f"http://127.0.0.1:{server.server_port}/api", ORGO_CUA_PAIR_CODE=PAIR)
        out, err = io.StringIO(), io.StringIO()
        code = orgo_cua.main(["up", "--pair"], env=env, out=out, err=err, sleep=lambda s: None, stdin=io.StringIO(""))
        self.assertEqual(code, 0, err.getvalue())
        (form,) = uploads
        self.assertEqual(form["file"], PAIR.encode())
        self.assertEqual(form["destPath"], b"/root/.orgo-cua-pair")
        self.assertEqual(form["workspaceId"], WS.encode())
        self.assertIn(form["desktopId"].decode(), self.fake.desktops)
        for command in self.fake.bash_commands:
            self.assertNotIn(PAIR, command)
        self.assertNotIn(PAIR, out.getvalue() + err.getvalue())
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())


class TlsTests(unittest.TestCase):
    """2026-10-20: python.org Python 3.12 on a maintainer's Mac had 0 trusted CAs, so every
    https call to Orgo failed with CERTIFICATE_VERIFY_FAILED."""

    def setUp(self):
        orgo_cua.tls_context.cache_clear()
        orgo_cua.opener.cache_clear()
        self.addCleanup(orgo_cua.tls_context.cache_clear)
        self.addCleanup(orgo_cua.opener.cache_clear)

    @unittest.skipUnless(Path(orgo_cua.SYSTEM_CA_BUNDLE).is_file(), "no system CA bundle here")
    def test_an_empty_python_trust_store_falls_back_to_the_system_bundle(self):
        import ssl
        from unittest import mock

        empty = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies, trusts nothing
        with mock.patch.object(orgo_cua.ssl, "create_default_context", return_value=empty):
            ctx = orgo_cua.tls_context()
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_every_request_uses_the_verified_context(self):
        import urllib.request

        orgo_cua.opener.cache_clear()
        self.addCleanup(orgo_cua.opener.cache_clear)
        (https,) = [h for h in orgo_cua.opener().handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertIs(https._context, orgo_cua.tls_context())


class RedirectTests(unittest.TestCase):
    """urllib copies the Authorization header onto a redirect to any host; the kit must not
    (2026-10-24: the screenshot link and Orgo's storage can redirect to object storage)."""

    def serve(self, routes):
        import http.server
        import threading

        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                status, location = routes(self.path)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", seen

    def test_the_key_is_dropped_on_a_redirect_to_another_host(self):
        other, other_seen = self.serve(lambda path: (200, None))
        api, api_seen = self.serve(lambda path: (302, other + "/elsewhere") if path == "/go" else (200, None))
        status, body = orgo_cua.urllib_http("GET", api + "/go", {"Authorization": f"Bearer {KEY}"}, None, 5)
        self.assertEqual((status, body), (200, b"ok"))
        self.assertEqual(api_seen, [("/go", f"Bearer {KEY}")])
        self.assertEqual(other_seen, [("/elsewhere", None)])

    def test_the_key_stays_on_a_redirect_within_the_host(self):
        api, seen = self.serve(lambda path: (302, "/target") if path == "/go" else (200, None))
        orgo_cua.urllib_http("GET", api + "/go", {"Authorization": f"Bearer {KEY}"}, None, 5)
        self.assertEqual(seen, [("/go", f"Bearer {KEY}"), ("/target", f"Bearer {KEY}")])

    def test_https_never_redirects_to_http(self):
        import urllib.error
        import urllib.request

        req = urllib.request.Request("https://orgo.test/api/x", headers={"Authorization": f"Bearer {KEY}"})
        with self.assertRaises(urllib.error.HTTPError):
            orgo_cua.KeyKeepingRedirects().redirect_request(req, None, 302, "Found", {}, "http://orgo.test/api/y")


if __name__ == "__main__":
    unittest.main()
