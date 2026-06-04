# clanker

Spin up a disposable cloud VM, drop an AI coding agent (**Codex** or **Claude Code**) into it to work
a CTF challenge, and watch/steer it from a local web UI — then throw the VM away.

- Pluggable **cloud providers** (GCP, DigitalOcean) and **agent backends** (Codex, Claude Code).
- A small HTTP **control plane** on each VM — no SSH on the hot path.
- A web UI with a live **chat transcript**, steering (any pane/subagent), per-run **VPN status**, and
  artifact browsing.

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

# d) get a toolbox image onto the provider (once; rebuild when the Dockerfile changes)
./scripts/ctfvm image push-registry --provider digitalocean   # …or build a local archive:
./scripts/ctfvm image build-local                             # then use --use-local-image
```

## 2. Start a run

```bash
./scripts/ctfvm start --dir ./challenge --desc "heap UAF chal" --agent codex
./scripts/ctfvm start --provider digitalocean --zone nyc3 --agent claude-code \
    --dir ./challenge --use-local-image --no-vpn
```

`--agent` / `--model` default from `.env` (`CTFVM_AGENT` / `CTFVM_MODEL`). A challenge dir may contain
`description.txt` and `ideas.txt`, which are picked up automatically.

## 3. Watch & steer it

```bash
python -m clanker serve                 # web UI at http://127.0.0.1:8765
# or from the terminal:
python -m clanker runs                  # list runs
python -m clanker status --run-id <id>  # one run's status
./scripts/ctfvm attach                  # raw tmux (break-glass)
```

In the UI: **Transcript** is the default tab (the agent's real chat — messages, tool calls, results).
The **Panes** tab has a steer box that types into the selected target — the supervisor or any subagent
(Enter sends, Shift+Enter is a newline); if the agent is mid-turn its own CLI queues your message.
Buttons cover Ctrl-C / Trust / Approve / Deny; the header has mark-solved/blocked, artifact
preview/download, and a per-run **VPN status** chip.

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

Runs the agent on a committed sample challenge, asserts the expected flag lands in findings, then
**auto-destroys** the VM. Costs a few cents / a few minutes. Good first check that auth + image +
provisioning all work. Sample challenges live in [`examples/smoke-challenges/`](examples/smoke-challenges/).

**Fan out a folder of challenges — one VM each** (each immediate subfolder is a challenge):

```bash
python -m clanker fanout ./ctf-challenges --provider digitalocean --agent codex
# or from the web UI: "+ New run" -> tick "deploy folder — each subfolder is its own run"
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

> Codex note: a `codex login` subscription token is short-lived and **rotates** — copying it to a VM
> works but can expire mid-run, and one subscription can't safely fan out across many concurrent VMs.
> For unattended/parallel Codex, an `--api-key` profile (`OPENAI_API_KEY`, doesn't rotate) is sturdier.

**Reach a LAN / internal challenge network from the agent's VM (WireGuard VPN):**

The agent runs on a cloud VM that can't see the competition LAN. clanker tunnels the VM to **your
laptop** (which *is* on the LAN) and NATs the routed subnets out your laptop's interface — so the agent
reaches internal services as if it were you. Bringing the tunnel up is a **local privileged step** (it
needs `sudo` for `wg-quick`/`pfctl` on the machine that's on the LAN), so it does **not** run from the
web UI or over ngrok — the UI shows status + the exact command instead.

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
- **At a venue where the LAN is on Ethernet but internet is on Wi-Fi**, the NAT egress auto-detects the
  *default-route* interface (usually Wi-Fi). Override it to the Ethernet one:
  `CTFVM_VPN_EGRESS_IF=enX ./scripts/ctfvm vpn --run-id <id> up` (find `enX` via `ifconfig`/`ipconfig getifaddr enX`).
- The focused run's header in the UI shows **VPN connected · `<iface>` → `<cidrs>`** or the `vpn up` command to run.

**Share the UI for a CTF (ngrok, token baked into the link):**

```bash
python -m clanker share        # prints https://<public>.ngrok.app/?token=...  (sets a cookie; just open it)
```

**Add an IDA MCP server / pick a model / change the control port:** all in `.env`
(`CTFVM_DEFAULT_IDA_MCP_URL`, `CTFVM_MODEL`, `CTFVM_CONTROL_PORT`); `config show` confirms it took.

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
| [docs/BUGS.md](docs/BUGS.md) · [docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) | Verified bugs; migration status |
| [CLAUDE.md](CLAUDE.md) | Repo layout & conventions (for contributors) |

Run the tests: `for t in tests/test_*.py; do python3 "$t"; done` (stdlib `unittest`, no deps).
