# orgo-cua

Run [Cua](https://github.com/trycua/cua) on an [Orgo](https://orgo.ai) cloud computer.

Orgo rents always-on Linux desktops (Ubuntu 24.04, xfce) by the hour. This kit installs
Cua's own computer service, `cua-spacesd`, straight onto an Orgo desktop and joins it to
your cua.ai account. The computer then shows up in `cua spaces ls` and the Cua Spaces app
like any computer you own: your agents can see and drive its real desktop, and Orgo's own
API keeps working on the same screen. No Docker, no change to Cua.

## Quickstart

You need Python 3.9 or newer, an Orgo account with an API key, and (to pair) the `cua`
CLI signed in to a cua.ai account; a free one is enough.

```sh
git clone https://github.com/OrgoAI/orgo-cua && cd orgo-cua
export ORGO_API_KEY=...  ORGO_WORKSPACE_ID=...    # your Orgo API key and a workspace id
./orgo-cua up --name my-cua-host                  # makes the computer and sets Cua up on it (~1 minute)
```

`ORGO_WORKSPACE_ID` is the id of an Orgo workspace you own; `GET
https://www.orgo.ai/api/workspaces` with your key lists them.

## Pair it with your Cua account

```sh
cua host pair --name my-cua-host                  # on any signed-in device: prints a one-time code
./orgo-cua up --name my-cua-host --pair           # paste the code when asked (or set ORGO_CUA_PAIR_CODE)
cua spaces ls                                     # the Orgo computer is listed as relay:<id>
```

The code also comes from the Cua Spaces app (**Add a computer**). From then on the computer
is yours in Cua: `cua do switch relay:<id>`, `cua do screenshot`, `cua do click X Y`, or
let an agent drive it.

## Commands

| Command | What it does |
|---|---|
| `./orgo-cua up [--name N] [--ram GB] [--cpu N]` | Create the computer (or reuse the one with that name) and set Cua up on it. Default 1 vCPU / 4 GB. Safe to run again: it changes only what is missing. |
| `./orgo-cua up --name N --pair` | The same, then join your Cua account with a one-time code. |
| `./orgo-cua status [--name N]` | Show whether Cua is ready on the computer. |
| `./orgo-cua unpair [--name N]` | Take the computer out of your Cua account (Cua's own `cua host remove`, run inside it). Cua keeps running on it, local-only. |
| `./orgo-cua down [--name N]` | Delete the computer, only if `up` created it. A paired computer leaves your Cua account first. |

Exit codes: 0 ready, 1 not ready, 2 usage or configuration, 3 Orgo API error. `up`
records the computers it created in `~/.config/orgo-cua/state.json` (ids only). Settings
come from the environment only: `ORGO_API_KEY`, `ORGO_WORKSPACE_ID`, and optionally
`ORGO_BASE_URL` (default `https://www.orgo.ai/api`; for example
`https://staging.orgo.ai/api`).

## How it works

`up` sends `guest/setup-cua-host.sh` through Orgo's bash endpoint. Inside the computer, as
root, it downloads the pinned Cua CLI and cua-spacesd, checks their sha256, runs `cua host
setup` against the Orgo desktop (display `:99`), and keeps cua-spacesd running with
supervisord (Orgo computers have no systemd). Before pairing, cua-spacesd listens on
`127.0.0.1:3211` inside the computer only; after pairing it dials out to Cua's relay as
your account. When setup reports `xz` missing, `up` installs Ubuntu's `xz-utils` in a
second call: Cua's agent installs unpack Node from a `.tar.xz` and fail without it.

| Component | Version | Checked against |
|---|---|---|
| Cua CLI | 0.7.0 | sha256 in trycua/cua release `cua-sdk-v0.7.0`, `release-artifacts.json` |
| cua-spacesd | 0.8.0 | sha256 in release `cua-spacesd-v0.8.0`, `<archive>.sha256` |

To upgrade, change the versions and hashes at the top of `guest/setup-cua-host.sh`. Files
land in `/opt/orgo-cua`, the supervisord program in
`/etc/supervisor/conf.d/cua-spacesd.conf`, Cua's state in `/root/.cua`, logs in
`/var/log/orgo-cua` (0700).

## Security

- **The Orgo API key** is read from the environment only, sent only as a Bearer header,
  only over https and only to Orgo's API host (never across a redirect to another host),
  and never printed.
- **The Cua pairing code** never appears on a command line or in a bash request to Orgo:
  it is uploaded as a file, and the computer reads it and deletes it at once. Cua accepts
  the code only as an argument (`cua host setup --pair CODE`), so it is visible in the
  process list of that one short-lived process inside the computer. Codes work once and
  expire after 30 minutes by default.
- **cua-spacesd's local token is a root shell on the computer.** It stays in
  `/root/.cua/host/env-token` (0600) and never leaves the computer; the setup log that
  would show it is scrubbed (and kept empty if scrubbing fails).
- Anyone you share the computer with in Cua can control its desktop, as root. Orgo
  computers are single-user VMs: give each person or agent their own.
- **Cua telemetry** is off for everything the kit runs.
- `down` deletes only a computer `up` created, by its exact id, and refuses if it was
  renamed.

## Limits

- **Background input.** Orgo's desktop runs Xtigervnc with no `/dev/uinput`, so Cua's
  "background" clicks and key presses (input without moving the pointer or focus) do not
  reach GTK apps such as the terminal or the file manager, or Chrome; Cua refuses them.
  Plain X apps take them. Tell agents to use foreground delivery (`delivery_mode:
  "foreground"`); `cua do` already does.
- Tested on Orgo Linux x86_64 (Ubuntu 24.04, the one Linux Cua lists as supported). The
  arm64 pins are included but untested.
- A relay session counts toward your Cua plan's remote hours.
- Cua and cua-spacesd are Cua's software under Cua's licenses; the kit downloads their
  official releases and checks the hashes.

## Cost

Orgo bills the computer, not this kit. From orgo.ai/pricing.md (read 2026-10-20): hourly
Medium (2 vCPU, 8 GB) is $0.06/hr running and $0.004/hr stopped; the Starter plan is
$29/month for one computer, and each extra computer is $32.50/month. The default 1 vCPU /
4 GB computer was enough in every test. `down` stops the bill.

## Maintainers

```sh
python3 -m unittest discover -s tests      # no network: fake Orgo API, fake cua CLI, bash stand-ins
shellcheck guest/*.sh scripts/*.sh
bash scripts/live-test.sh                  # one real run of the kit
bash scripts/agent-test.sh                 # one real Cua agent (Claude Code) run on an Orgo desktop
bash scripts/morning-proof.sh              # the release check: agent run with screenshots, then pairing
```

The three live scripts work only against Orgo's test environment, each in a scratch
workspace they delete; computers in other workspaces are counted, never touched. They read
keys from the macOS keychain (`ORGO_CUA_TEST_ORGO_KEYCHAIN` and
`ORGO_CUA_TEST_ANTHROPIC_KEYCHAIN`, each `service/account`) and hand them to a helper on
stdin; nothing is printed or saved. The agent runs send the Anthropic key into the scratch
computer encrypted to a one-time key pair, and it is gone when the computer is deleted.
`morning-proof.sh` also needs the `cua` CLI signed in to a cua.ai account. Results go to
`results/` (or `ORGO_CUA_RESULTS`), each ending `RESULT: PASS` or `FAIL`.

## License

MIT, see [LICENSE](LICENSE).
