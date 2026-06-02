# Clanker — Target Architecture

This document describes the **target state** after the refactor. It is the architectural source of
truth. Companion documents:

- [`AGENTS.md`](AGENTS.md) — the agent-backend layer (Codex + Claude Code), auth, and subagents.
- [`API.md`](API.md) — the HTTP API surface of the local server.
- [`BUGS.md`](BUGS.md) — verified bug catalogue (with refutations of overstated claims).
- [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md) — the staged migration plan and feature keep/cut list.

The operational guides [`CTFVM.md`](CTFVM.md) and
[`HTTP_CONTROL_PLANE_ARCHITECTURE.md`](HTTP_CONTROL_PLANE_ARCHITECTURE.md) remain user-facing; this
document supersedes them for architectural intent.

---

## 1. What Clanker is

Clanker is a **local-first CTF automation platform**. It:

1. Provisions ephemeral VMs on a **cloud provider** (GCP or DigitalOcean).
2. Bootstraps each VM with a small HTTP **control plane** and a Docker **toolbox** container.
3. Runs an AI coding **agent** (Codex CLI or Claude Code) headless inside the toolbox, in tmux.
4. Lets the operator **monitor and steer** every run — and every subagent — from a local web UI
   and a CLI.

**Invariant:** everything runs on the operator's machine or on cloud VMs the operator controls.
There is no shared SaaS coordination layer and no shared state between operators.

---

## 2. The two new abstractions

The refactor is organized around making two things pluggable instead of hard-coded:

| Abstraction      | Today                          | Target                                                   |
|------------------|--------------------------------|----------------------------------------------------------|
| **CloudProvider**| GCP + DO, partly shared bash   | One Python ABC; `gcp`, `digitalocean` implementations    |
| **AgentBackend** | Codex only, hard-coded         | One Python ABC; `codex`, `claude-code` implementations   |

`CloudProvider` answers *"where does the work run?"* `AgentBackend` answers *"what agent does the
work, and how do we authenticate, configure, launch, and steer it (including subagents)?"*

These are **orthogonal**: any agent backend can run on any cloud provider. See
[`AGENTS.md`](AGENTS.md) for the full `AgentBackend` contract — it is the centerpiece of this
refactor and the reason for the Python port.

---

## 3. One shared core, thin consumers

Today the logic is split across a 4,200-line bash CLI and a ~3,800-line Python UI that duplicate
provisioning, state, and control-plane concepts. The target collapses all shared logic into **one
Python package**, consumed by every entry point:

```
                         ┌───────────────────────────────────────────┐
                         │            clanker (Python core)           │
                         │                                            │
   CLI  ───────────────▶ │  models · state · control-plane client    │
   Web UI / server ────▶ │  CloudProvider(abc) · AgentBackend(abc)   │ ──▶ cloud APIs (gcloud/doctl)
   (any future surface) ▶ │  provisioning · snapshot · steering · jobs │ ──▶ VM control plane (HTTP)
                         │  artifacts · vpn · images                  │
                         └───────────────────────────────────────────┘
```

**Why Python, not bash:** the web server and the agent layer are Python-native, and the entire goal
is that every consumer shares *one* control-plane client, *one* set of models, *one* provider layer,
and *one* agent layer. Bash cannot expose typed, testable, importable modules to a web server. Bash
is retained only **on the VM**, where it is the right tool and may run before Python is present
(`vm/startup.sh`, `runner/*.sh`).

The CLI becomes a **thin argument parser** over the core — no business logic of its own. It is
ported incrementally (see [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md) Phase 1–2); the existing bash
entrypoint keeps working until each command is migrated.

---

## 4. Two-plane model (unchanged in spirit)

```
┌─────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE   (local core ⇄ VM control-plane port, default 443)  │
│  - Provisioning: upload challenge files, runner scripts, agent config│
│  - Monitoring:   snapshot queries (tmux panes, findings, artifacts)  │
│  - Steering:     tmux send-keys, inject messages, trust prompts      │
│  Transport: HTTP Basic Auth over a provider-firewalled TCP port      │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  WORK PLANE   (inside VM → inside ctf-toolbox container)            │
│  - Agent supervisor session in tmux (Codex or Claude Code)           │
│  - Subagent sessions, each mirrored into its own tmux session        │
│  - Challenge artifacts, findings.md, supervisor.log                  │
│  - GDB MCP server, optional remote IDA MCP endpoint                  │
└─────────────────────────────────────────────────────────────────────┘
```

**Break-glass SSH** (`attach`, `shell`, `vscode`) is intentionally *outside* the control plane. It
is a human-operated path and must never be on the monitoring/steering hot path. See §9.

---

## 5. Target component map

### 5.1 Local Python core — `clanker/`

```
clanker/
├── __init__.py
├── cli.py                  # Thin argparse/click entrypoint → core (replaces scripts/ctfvm)
├── config.py               # Settings: .env, .ctfvm/config.json, CLI flags, precedence rules
├── models.py               # ALL wire/data shapes (dataclasses). Single source of truth.
├── state.py                # RunRegistry: load/merge/persist .ctfvm/*.json, locked, cached
├── controlclient.py        # Typed HTTP control-plane client (exec/upload/download/health)
├── provisioning.py         # start/destroy run lifecycle orchestration
├── snapshot.py             # Remote snapshot script builder + JSON parser
├── artifacts.py            # Artifact list/preview/download/bundle + path validation
├── steering.py             # tmux send-text / send-keys / trust, with target validation
├── jobs.py                 # Spawn-job tracker (bounded, thread-safe)
├── vpn.py                  # WireGuard lifecycle (was cmd_vpn_* in bash)
├── images.py               # Toolbox image build/push/registry resolution
├── providers/              # CloudProvider abstraction
│   ├── base.py             #   CloudProvider ABC
│   ├── gcp.py
│   └── digitalocean.py
├── agents/                 # AgentBackend abstraction — see AGENTS.md
│   ├── base.py             #   AgentBackend ABC
│   ├── codex.py
│   └── claude_code.py
├── server/                 # Local HTTP server (UI + API) — see API.md
│   ├── app.py              #   route table, request/response helpers
│   ├── routes.py           #   thin handlers → core services
│   └── frontend/
│       └── index.html      #   self-contained SPA, no longer embedded in Python strings
```

### 5.2 VM side — `vm/` (stays bash/Python, hardened not rewritten)

```
vm/
├── startup.sh              # Bootstrap: Docker, ctf user, control-plane service
└── control_server.py       # HTTP control plane (exec, upload, upload-tar, download, healthz)
```

### 5.3 Container side — `images/ctf-toolbox/`

```
images/ctf-toolbox/
├── Dockerfile              # lean + full variants; now installs BOTH codex and claude CLIs
├── install-python-runtime.sh
├── agent-config/           # was codex-config/ — now per-backend templates (see AGENTS.md)
│   ├── codex/              #   config.toml, roles/*.toml
│   └── claude/             #   settings.json, agents/*.md, .mcp.json
└── mcp/
    └── gdb_mcp.py          # Persistent GDB stdio MCP server (gets a thread/async lock — bug fix)
```

### 5.4 Runner — `runner/` (on VM, outside container)

```
runner/
├── supervisor.sh           # Starts the agent session via the selected backend's launch command
└── subagent-tmux-bridge.sh # Mirrors each subagent into its own tmux session (both backends)
```

`spawn-idea-workers.sh` is **removed** (superseded by supervisor-native subagents — see cut list).

---

## 6. Module responsibilities (core)

### `models.py` — data contracts
All shared structures live here; nothing else defines a wire shape.
- `RunRecord` (frozen) — canonical run descriptor: provider, agent_backend, run_id, instance,
  zone, project, ip, control_*, started_at, remote_run_dir.
- `Snapshot`, `PaneSnapshot`, `SnapshotMetrics`, `ChallengeState`, `ExplicitStatus`.
- `ArtifactEntry`, `ExecResult`, `SpawnJob`, `Subagent`.

### `state.py` — `RunRegistry`
The single owner of the known-run set. Loads `.ctfvm/*.json`, merges provider discoveries by
identity key, persists, and caches per-run runtime status (TTL, default 10 s). **All `RLock`-guarded
— fixes the discovery-cache TOCTOU race.** No other module touches `.ctfvm/*.json` directly.

### `controlclient.py` — typed control-plane client
Replaces the stringly-typed `ctfvm_control_client.py`. Methods `exec / upload_file / upload_tar /
download_file / health` returning typed results, raising typed exceptions
(`ControlPlaneAuthError`, `ControlPlaneTimeout`, `ControlPlaneError`). Connect+read timeouts on
every call.

### `providers/base.py` — `CloudProvider` ABC
```python
class CloudProvider(ABC):
    def create(self, spec: RunSpec) -> RunRecord: ...
    def destroy(self, run: RunRecord) -> None: ...
    def get_status(self, run: RunRecord) -> str: ...
    def discover_runs(self) -> list[RunRecord]: ...
    def ssh_break_glass(self, run: RunRecord, argv: list[str]) -> None: ...  # interactive only
    def prepare_registry_pull(self, run: RunRecord) -> None: ...
```
SSH appears **only** in `ssh_break_glass`. The monitoring/steering/artifact paths use the control
client exclusively (see §9, Invariant 1).

### `agents/base.py` — `AgentBackend` ABC
The heart of the refactor. Full contract in [`AGENTS.md`](AGENTS.md). In brief:
```python
class AgentBackend(ABC):
    name: str                                   # "codex" | "claude-code"
    def materialize_auth(self) -> AuthMaterial: ...      # local creds → injectable form
    def render_config(self, ctx) -> list[StagedFile]: ...# config.toml / settings.json + MCP + skills
    def supervisor_launch_cmd(self, ctx) -> str: ...     # how supervisor.sh starts the agent
    def spawn_subagent(self, ctx, hypothesis) -> Subagent: ...
    def detect_subagents(self, run) -> list[Subagent]: ...
    def steer(self, run, target, text) -> None: ...
```

### `snapshot.py`, `artifacts.py`, `steering.py`, `jobs.py`
Extracted, single-responsibility versions of today's tangled `service.py`. Snapshot uses a **single
JSON envelope** instead of marker-splitting (bug fix, §10). Artifacts and steering validate paths
and tmux targets *before* any remote call.

### `server/` — local HTTP server
Thin route handlers that parse a request, call a core service, and serialize the result. No business
logic. Serves the SPA at `/` from a real `index.html` file. Full surface in [`API.md`](API.md).

---

## 7. Data flow

### Provisioning
```
clanker start --provider gcp --agent claude-code --dir ./chal
  → CloudProvider.create() makes the VM (gcloud/doctl)
  → startup.sh: Docker, control plane, ctf user
  → core polls GET /healthz until ready
  → AgentBackend.materialize_auth()  → token/creds to inject
  → AgentBackend.render_config()     → staged config/MCP/skills files
  → control client uploads: challenge tar, runner scripts, prompts, agent config
  → control client exec: docker pull + docker run ctf-toolbox
  → control client exec: supervisor.sh (uses AgentBackend.supervisor_launch_cmd())
  → RunRegistry persists .ctfvm/<run_id>.json (now includes agent_backend)
```

### Monitoring
```
UI polls GET /api/v1/runs/{run_id}
  → routes resolve RunRecord from RunRegistry
  → snapshot.build_script()  → remote bash emitting ONE JSON object
  → controlclient.exec()     → control plane only (no SSH fallback)
  → snapshot.parse()         → Snapshot dataclass
  → ChallengeState derived; serialized to the response envelope
```

### Steering
```
UI POST /api/v1/runs/{run_id}/panes/send {target, text, enter}
  → steering.validate_target()  (reject before hitting the VM)
  → base64-encode text          (no remote shell quoting)
  → controlclient.exec()        tmux send-keys with decoded payload
```

---

## 8. State file (`.ctfvm/<run_id>.json`)

Backwards-compatible with the current CLI format, **plus one new field**: `agent_backend`
(`"codex"` | `"claude-code"`, defaulting to `"codex"` when absent so old runs still load).

```json
{
  "provider": "gcp",
  "agent_backend": "claude-code",
  "run_id": "20250115-103000",
  "instance": "ctfvm-myctf-20250115-103000",
  "zone": "us-central1-a",
  "project": "my-gcp-project",
  "ip": "1.2.3.4",
  "started_at": "2025-01-15T10:30:00Z",
  "control_scheme": "http",
  "control_host": "1.2.3.4",
  "control_port": "443",
  "control_user": "ctfvm",
  "control_password": "<random>",
  "remote_run_dir": "/home/ctf/run"
}
```

`RunRegistry` reads these into `RunRecord`; both the CLI and the server write through `RunRegistry`
(no more 22-positional-arg `write_state` in bash).

---

## 9. VM control server (`vm/control_server.py`)

Endpoints are **stable** (no protocol break):

| Method | Path                | Auth  | Purpose                       |
|--------|---------------------|-------|-------------------------------|
| GET    | `/healthz`          | none  | Uptime + provider info        |
| POST   | `/exec`             | basic | Execute a shell command       |
| POST   | `/files/upload`     | basic | Upload a single file          |
| POST   | `/files/upload-tar` | basic | Extract a tar to a destination|
| GET    | `/files/download`   | basic | Download a single file        |

Hardening in this refactor (details in [`BUGS.md`](BUGS.md)):
- Add structured request logging to stderr (`<ts> <method> <path> <status> <ms>`); today
  `log_message` is a no-op — there is no audit trail.
- Separate body-size cap for `/exec` JSON (10 MB) vs file uploads (1 GB).
- Keep the existing (already-correct) tar member validation and timeout-kill handling.

---

## 10. Challenge state machine

```
            explicit "solved"  ─────────────▶ solved
            explicit "blocked" ─────────────▶ blocked
  [run] ──▶ no tmux / HALTED pattern / status inactive ─▶ halted
            runtime active, no file activity >15 min ───▶ stalled
            recent file activity (<5 / <15 min) ────────▶ progressing
```

Activity is detected by newest mtime of `findings.md`, `supervisor.log`, and the newest artifact
(writes are append-only in practice, so mtime is sufficient). This logic moves verbatim into
`snapshot.py`/`models.py`.

---

## 11. Security model

| Layer                 | Mechanism                         | Status                                  |
|-----------------------|-----------------------------------|-----------------------------------------|
| Control-plane auth    | HTTP Basic Auth per run           | Credentials in `.ctfvm/<run_id>.json`   |
| Network perimeter     | Provider firewall rules           | Restrict control port to operator IP    |
| Artifact path safety  | `sanitize_relpath()`              | **Already correct today** (see below)   |
| Tmux target safety    | allowlist regex + `shlex.quote()` | **Already correct today** (see below)   |
| Tar extraction        | member validation before extract  | **Already correct today**               |
| Command execution     | caller passes pre-quoted commands | No shell-injection in core call sites   |
| Agent auth on VM      | injected token, wiped on destroy  | Codex OAuth sync / Claude OAuth token   |
| UI access             | binds 127.0.0.1, no auth          | Local-only by design                    |

> **Correction to earlier drafts.** Prior planning notes listed "artifact path traversal" and "tmux
> target shell injection" as *critical* bugs. They are **not exploitable** in the current code:
> `sanitize_artifact_relpath` rejects any `..` component and requires an `artifacts/` prefix, and
> `safe_target` enforces `^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$` before the value is additionally
> `shlex.quote()`d. These validations are *preserved* in the refactor, not introduced by it. See
> [`BUGS.md`](BUGS.md) for the evidence.

Out of scope (firewall is the perimeter): TLS on the control plane, multi-user UI auth, control-plane
rate limiting.

---

## 12. Key invariants

1. **Control plane is the only hot path.** Monitoring, steering, and artifacts use the control
   client exclusively. SSH is reachable *only* from break-glass commands (`attach`, `shell`,
   `vscode`). Missing control credentials raise an error — they do not silently fall back to SSH.
2. **`RunRegistry` is the single source of truth.** No other module reads or writes `.ctfvm/*.json`.
3. **All wire shapes live in `models.py`.** Service functions return dataclasses, never inline dicts.
4. **One snapshot, one JSON object.** No marker-based string splitting (fixes the collision bug).
5. **Validate before you call.** Tmux targets and artifact paths are validated locally; invalid
   input returns 400 without touching the VM.
6. **CloudProvider and AgentBackend are orthogonal and pluggable.** Adding a provider or an agent is
   a new file implementing an ABC — no edits to the consumers.
7. **The VM scripts stay bash; everything else is Python.** The core is importable by the CLI and
   the server alike.
