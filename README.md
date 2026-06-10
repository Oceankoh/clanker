# clanker

Spin up a disposable cloud VM, drop an AI coding agent (**Codex** or **Claude Code**) into it to work
a CTF challenge, and watch/steer it from a local web UI — then throw the VM away.

- Pluggable **cloud providers** (GCP, DigitalOcean) and **agent backends** (Codex, Claude Code).
- A pre-baked **golden image** → fast boots, agent runs **directly on the host** (no Docker).
- **One VM per challenge**, or a reusable **worker** VM that hosts many — same setup either way.
- A small HTTP **control plane** on each VM (no SSH on the hot path) + a web UI with a live **chat
  transcript**, steering, per-run **VPN status**, and artifact browsing.

> **Two commands, one system.** It's a strangler migration: the two CLIs share the same `.ctfvm/`
> state, config, and agent/provider abstractions — they just split by job. Use whichever the table
> says; `clanker` shells out to `ctfvm` for the actual VM spin-up.
>
> | Job | Command |
> |-----|---------|
> | Spin up / tear down a VM, VPN, break-glass `attach`/`shell`, build/push images | `./scripts/ctfvm` (bash) |
> | Watch / steer, web UI, config, auth, agents, fanout, fetch results | `python -m clanker` (Python) |
>
> Full details: [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md).

---

## 1. One-time setup

```bash
# a) a cloud CLI, authenticated
gcloud auth login                 # GCP  …or…
doctl auth init                   # DigitalOcean

# b) configure defaults (single file — every knob is CTFVM_*)
cp .env.example .env && $EDITOR .env
python -m clanker config show     # verify what resolved, and from where

# c) authenticate the agent you want
codex login                       # Codex (syncs ~/.codex to the VM)
python -m clanker auth claude     # Claude (stores a setup-token; never copies creds)

# d) bake the golden VM image once (tools + agent CLIs pre-installed, no Docker, no creds)
#    IDA requires CTFVM_IDA_INSTALLER_URL or CTFVM_IDA_INSTALLER_PATH in .env.
python -m clanker image bake --provider digitalocean    # ~12-15 min; reused by every run/worker
python -m clanker image status                          # when it was built; re-bake to update tools
```

> After this, runs boot the golden image and run the agent **directly on the host** (no Docker). Blank
> golden image ⇒ stock distro + a slow per-boot install; `--use-local-image` forces the legacy container.

## 2. Start work — two models, same setup

```bash
# A) one VM per challenge (own VM, torn down when done)
./scripts/ctfvm start --dir ./challenge --desc "heap UAF chal" --agent codex

# B) a worker — one reusable VM that hosts many challenges, added on demand
python -m clanker worker spawn --count 3 --provider digitalocean
python -m clanker worker add worker-01 --dir ./challenge --agent codex
```

Both build the same prompt (`description.txt` + `ideas.txt` from the dir + shared instructions) and run
the agent dockerless on the golden image. `--agent` / `--model` / `--reasoning-effort` default from
`.env`. A worker hosts challenges in isolated sessions; `worker harvest <w>` saves every challenge's
output before you destroy it.

## 3. Watch & steer it

```bash
python -m clanker serve                 # web UI at http://127.0.0.1:8765
# or from the terminal:
python -m clanker runs                  # list runs
python -m clanker status --run-id <id>  # one run's status
./scripts/ctfvm attach                  # raw tmux (break-glass)
```

In the UI: **Transcript** (default) is the agent's real chat. **Panes** has a steer box that types into
any target — supervisor or subagent (Enter sends, Shift+Enter newline; mid-turn input queues). Buttons:
Ctrl-C / Trust / Approve / Deny. The header shows the run name + state, mark-solved/blocked (with note),
artifact preview/download, and **VPN status**. The sidebar pills auto-refresh with a freshness stamp.

## 4. Get results / tear down

```bash
python -m clanker fetch --run-id <id> --out ./out   # findings + artifacts + logs
./scripts/ctfvm destroy                             # delete the VM, wipe credentials
python -m clanker cleanup-state --prune-non-running # tidy local state files
```

---

## Recipes

**Verify the whole pipeline end-to-end** (provision → solve → teardown, on a real VM):

```bash
scripts/smoke.sh --agent claude-code --dir examples/smoke-challenges/01-strings
scripts/smoke.sh --agent codex       --dir examples/smoke-challenges/04-hidden   # flag in a dotfile
```

Runs the agent on a committed sample, asserts the flag lands in findings, then **auto-destroys** the VM.
Good first check that auth + image + provisioning work. Samples: [`examples/smoke-challenges/`](examples/smoke-challenges/).

**Exercise VPN + the gdb MCP** — heavier fixtures in [`examples/lab-challenges/`](examples/lab-challenges/):

```bash
# web-local: agent reaches a service on YOUR laptop, only over the VPN
scripts/ctfvm start --dir examples/lab-challenges/web-local      # then: vpn up + run web-local-serve.py
# pwn-overflow: x86-64 ret2win binary — exercises the gdb MCP + pwntools
scripts/ctfvm start --dir examples/lab-challenges/pwn-overflow
```

**Manage workers** (spawn empty VMs in §2, then):

```bash
python -m clanker worker ls                              # workers + hosted-challenge counts
python -m clanker worker show worker-01                  # its challenges
python -m clanker worker rm-challenge worker-01 pwn-01   # stop one challenge
python -m clanker worker harvest worker-01 --out ./out   # save every challenge's output BEFORE destroying
```

Each challenge is its own focusable run in the UI, grouped under its worker. Trade-off: challenges on
one worker share ports/packages (no container isolation) — see
[docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md](docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md).

**Fan out a folder — one VM per subfolder:**

```bash
python -m clanker fanout ./ctf-challenges --provider digitalocean --agent codex
# or in the UI: "+ New run" -> tick "deploy folder"
```

**List the agent backends + readiness:**

```bash
python -m clanker agents
#  codex        Codex CLI    model=gpt-5.5            auth=ready
#  claude-code  Claude Code  model=(backend default)  auth=ready
```

**Run multiple subscriptions side by side** — store one credential profile per account, pick per run:

```bash
python -m clanker auth claude --name alice                 # alice's Claude sub
python -m clanker auth codex  --name bob --api-key sk-...   # bob's Codex account
python -m clanker config profiles
# pick the profile per run: the UI spawn form's "Account / profile" field,
# or  ./scripts/ctfvm start --account alice --dir ./challenge
```

> Codex note: a `codex login` token rotates and can expire mid-run, and one sub can't safely fan out
> across many VMs. For unattended/parallel Codex, an `--api-key` profile (`OPENAI_API_KEY`) is sturdier.

**Reach a LAN / internal challenge network from the agent's VM (WireGuard VPN):**

The agent's cloud VM can't see the competition LAN. clanker tunnels it to **your laptop** (on the LAN)
and NATs the routed subnets out — so the agent reaches internal services as if it were you. Bring-up
needs **local `sudo`** (`wg-quick`/`pfctl`), so it runs from the CLI, not the UI (the UI shows the command).

```bash
# one-time: WireGuard tools (macOS also needs bash 4+, both via Homebrew)
brew install wireguard-tools bash

# VPN is on by default (CTFVM_VPN). A CLI start from a terminal auto-brings-up the tunnel;
# a UI/non-interactive spawn defers it (no tty for sudo) and the UI shows the command. Bring up
# the tunnel(s) locally:
./scripts/ctfvm vpn up                      # ALL active runs, one sudo session  (← typical)
./scripts/ctfvm vpn --run-id <id> up        # just one run
./scripts/ctfvm vpn --run-id <id> status    # or 'down'
```

- Default routed CIDRs are all RFC1918 (`CTFVM_VPN_CIDRS`); the VM's own subnets are safe (longest-prefix).
- The tunnel routes IPs; **internal-hostname DNS** is forwarded through your laptop **by default**
  (`CTFVM_VPN_DNS=auto`) so the agent resolves what you can (LAN/internal + public). The UI top bar shows a
  **DNS proxy** chip (red ⚠ if it's not running). `CTFVM_VPN_DNS=off` to disable. See [docs/CTFVM.md](docs/CTFVM.md#resolving-internal-hostnames-dns).
- **At a venue where the LAN is on Ethernet but internet is on Wi-Fi**, the NAT egress auto-detects the
  *default-route* interface (usually Wi-Fi). Override it to the Ethernet one:
  `CTFVM_VPN_EGRESS_IF=enX ./scripts/ctfvm vpn --run-id <id> up` (find `enX` via `ifconfig`/`ipconfig getifaddr enX`).
- The focused run's header in the UI shows **VPN connected · `<iface>` → `<cidrs>`** or the `vpn up` command to run.

**Share the UI for a CTF (ngrok, token baked into the link):**

```bash
python -m clanker share        # prints https://<public>.ngrok.app/?token=...  (sets a cookie; just open it)
```

**IDA MCP / model / reasoning effort / control port:** all in `.env` (`CTFVM_DEFAULT_IDA_MCP_URL`,
`CTFVM_MODEL`, `CTFVM_REASONING_EFFORT`, `CTFVM_CONTROL_PORT`); `config show` confirms it took.

**Push a file to a running run** (confined to the run's workspace by default):

```bash
python -m clanker upload --run-id <id> ./exploit.py                 # -> workspace/exploit.py
python -m clanker upload --run-id <id> ./libs --tar remote/libs     # tar a dir, extract remotely
python -m clanker upload --run-id <id> ./key --allow-abs /etc/x     # opt out of workspace confinement
# or in the UI artifacts tab: "Upload file", or "Upload folder (tar)" — picks a folder and
# tars it in the browser, so worker "add challenge" also takes a folder directly.
```

**Continue manually then resume the agent:**

```bash
./scripts/ctfvm sync-down --out ./live   # pull the remote workspace
./scripts/ctfvm shell                    # hack on the VM directly
python -m clanker sync-down --run-id <id> # (Python equivalent)
```

---

## Add an agent backend (TL;DR)

Agents are pluggable. To add one (e.g. Gemini, opencode):

```python
# clanker/agents/mybackend.py
from .base import AgentBackend, AgentConfigSpec, AuthMaterial, StagedFile

class MyBackend(AgentBackend):
    name = "mybackend"; display_name = "My Agent"; cli_binary = "myagent"
    default_model = "..."
    transcript_glob = ".myagent/sessions/**/*.jsonl"   # where it writes session JSONL
    transcript_recursive = True

    def materialize_auth(self, settings) -> AuthMaterial: ...     # token/env or files to inject
    def render_config(self, spec) -> list[StagedFile]: ...        # on-VM config files
    def supervisor_launch_cmd(self, spec) -> str: ...             # how supervisor.sh starts it
```

Then register it (one line — the factory, `clanker agents`, `--agent` choices, and the spawn form
all read the registry):

```python
# clanker/agents/__init__.py
from .mybackend import MyBackend
register_backend(MyBackend, "my")
```

Add a transcript parser in `clanker/transcript.py` if its session format differs. Full contract:
[docs/AGENTS.md](docs/AGENTS.md).

## Docs

| Doc | What |
|-----|------|
| [docs/CTFVM.md](docs/CTFVM.md) | Full operational guide (every flag, VPN, registries, MCP) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Target architecture, the two ABCs, invariants |
| [docs/AGENTS.md](docs/AGENTS.md) | Agent backends: auth, config mapping, subagents |
| [docs/API.md](docs/API.md) | The `/api/v1` HTTP surface |
| [docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md](docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md) | Golden image, worker VMs, uploads (design + status) |
| [docs/BUGS.md](docs/BUGS.md) · [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) | Verified bugs; migration status |
| [CLAUDE.md](CLAUDE.md) | Repo layout & conventions (for contributors) |

Run the tests: `for t in tests/test_*.py; do python3 "$t"; done` (stdlib `unittest`, no deps).
