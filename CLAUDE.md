# clanker

Local-first CTF automation platform: provisions ephemeral cloud VMs, runs an AI coding agent
(**Codex** or **Claude Code**) headless inside a Docker toolbox over a small HTTP control plane, and
lets the operator monitor and steer every run — and every subagent — from a local web UI and CLI.

## Layout

```
clanker/            # shared Python core (platform-v2) — the source of truth for logic
├── models.py         # all wire/data shapes (dataclasses)
├── identity.py       # run normalization / dedup keys
├── state.py          # RunRegistry: load/merge/persist .ctfvm/*.json (one RLock)
├── config.py         # paths, tunables, Settings precedence (cli > .env > env > config > default)
├── controlclient.py  # typed HTTP control-plane client (exec/upload/download/health)
├── providers/        # CloudProvider ABC + gcp/digitalocean (discover + status)
├── agents/           # AgentBackend ABC + codex/claude_code (auth, config render, launch)
├── snapshot.py       # remote JSON-envelope gatherer + parser + challenge-state
├── steering.py       # tmux send-text/keys/trust/status (validate locally, base64 transport)
├── artifacts.py      # preview/download/bundle (read as ctf, realpath-guarded)
├── secretstore.py    # .ctfvm/secrets.json (Claude OAuth token)
├── commands.py       # ported read-only CLI command bodies
├── server/           # /api/v1 HTTP server (app/service/serialize/jobs) + frontend/index.html
└── __main__.py       # `python -m clanker` entrypoint

scripts/ctfvm       # legacy bash CLI — still the primary entrypoint for start/destroy/break-glass
vm/                 # on-VM: startup.sh (bootstrap) + control_server.py (HTTP control plane)
runner/             # on-VM: supervisor.sh (launches the agent) + subagent-tmux-bridge.sh
images/ctf-toolbox/ # the toolbox container (Dockerfile, gdb MCP, agent-config templates)
docs/               # ARCHITECTURE, AGENTS, API, BUGS, REFACTOR_PLAN, SMOKE_TEST, CTFVM
tests/              # stdlib unittest; run `for t in tests/test_*.py; do python3 "$t"; done`
```

## The two pluggable abstractions

- **CloudProvider** (`providers/`) — *where* work runs: gcp, digitalocean.
- **AgentBackend** (`agents/`) — *what* agent runs and how it's authenticated/configured/launched:
  codex, claude-code. A backend-neutral `AgentConfigSpec` renders to either Codex `config.toml` or
  Claude `settings.json` + `.mcp.json` + `.claude/agents`. See [docs/AGENTS.md](docs/AGENTS.md).

## Status (strangler migration)

The Python core (Phases 1–6) is built and unit-tested; `scripts/ctfvm` remains the entrypoint for
provisioning until the `start`/`destroy` port is wired and **smoke-tested against a live VM**. See
[docs/REFACTOR_PLAN.md](docs/REFACTOR_PLAN.md) for what's done vs. live-deferred, and
[docs/BUGS.md](docs/BUGS.md) for the verified bug catalogue (and refuted claims to preserve).

## Conventions

- The control plane is the only hot path; SSH is break-glass only (Invariant 1).
- `RunRegistry` is the sole reader/writer of `.ctfvm/*.json` (Invariant 2).
- All wire shapes live in `models.py` (Invariant 3).
- Validate tmux targets / artifact paths *before* any remote call (Invariant 5).
- Tests are stdlib `unittest`, no external deps. Add a regression test with every bug fix.
