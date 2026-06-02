# Agent Backend Layer (Codex + Claude Code)

This is the centerpiece of the refactor. It makes the in-VM AI agent **pluggable** so Clanker can run
either **Codex CLI** or **Claude Code** as a first-class, co-equal backend — including subagents that
are individually visible and steerable in their own tmux sessions on *both* backends.

It is the analogue of the `CloudProvider` abstraction: `CloudProvider` answers "where does work run?";
`AgentBackend` answers "what agent runs, and how do we authenticate, configure, launch, and steer it?"

> Facts below were verified against the live code and current Claude Code / Codex documentation. Exact
> flag names (e.g. permission-mode spellings, `--session-id`) should be re-verified against the
> installed CLI version at implementation time; the *architecture* does not depend on the exact spelling.

---

## 1. The `AgentBackend` contract

```python
class AgentBackend(ABC):
    name: str                      # "codex" | "claude-code"
    display_name: str

    # --- AUTH: turn local operator credentials into something injectable on the VM ---
    def materialize_auth(self, cfg: Settings) -> AuthMaterial: ...
    #   AuthMaterial = files to stage (e.g. ~/.codex/*) + env vars (e.g. CLAUDE_CODE_OAUTH_TOKEN)

    # --- CONFIG: produce the agent's on-VM config, MCP servers, skills, subagent defs ---
    def render_config(self, ctx: RunContext) -> list[StagedFile]: ...

    # --- LAUNCH: the command supervisor.sh runs to start the main session in tmux ---
    def supervisor_launch_cmd(self, ctx: RunContext) -> str: ...

    # --- SUBAGENTS: spawn / detect / list, so each is mirrored into its own tmux session ---
    def spawn_subagent(self, ctx: RunContext, spec: SubagentSpec) -> Subagent: ...
    def detect_subagents(self, run: RunRecord) -> list[Subagent]: ...

    # --- STEERING: inject a follow-up turn into a running (sub)agent session ---
    def steer(self, run: RunRecord, target: TmuxTarget, text: str) -> None: ...
```

Everything below specifies how `codex` and `claude-code` implement each method, and exactly where the
two must diverge.

---

## 2. Authentication

The operator authenticates **locally, once**; the backend materializes that into a form that can be
injected into the VM and wiped on `destroy`. Both modes are **configurable** via `.env` /
`.ctfvm/config.json` / CLI flags.

### 2.1 Codex (`materialize_auth`)
- **Default — OAuth session sync:** copy local `~/.codex` session material to the VM (today's
  `--auth-sync` flow). Permission-restricted on the VM; wiped on destroy.
- **Alternative — API key:** `OPENAI_API_KEY` env var injected instead of session sync
  (`--no-auth-sync` + key). Configurable.

### 2.2 Claude Code (`materialize_auth`)
Verified facts:
- **Copying `~/.claude/` or `~/.claude.json` does NOT work** across machines. Credentials are bound
  to the OS keychain (macOS) or a machine-specific `~/.claude/.credentials.json` (Linux). `.claude.json`
  holds MCP config, *not* auth. *(This confirms the operator's prior experience.)*
- **Default — subscription OAuth token:** the operator runs `claude setup-token` **locally** (one
  interactive browser consent), which prints a **~1-year OAuth token**. Clanker captures it and injects
  it on the VM as **`CLAUDE_CODE_OAUTH_TOKEN`**. No credential files are copied.
- **Alternative — console API key:** inject **`ANTHROPIC_API_KEY`**. Also covers gateway/proxy via
  `ANTHROPIC_AUTH_TOKEN`, and Bedrock/Vertex via `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX`.
  All configurable.

**Token capture UX.** `claude setup-token` is interactive, so it cannot run inside a background UI
job. Clanker handles this like local `sudo -v` for VPN today:
- `clanker auth claude` runs `setup-token` locally and stores the token in the OS keychain (preferred)
  or `.ctfvm/secrets.json` (gitignored, `0600`).
- `clanker start --agent claude-code` reads the stored token; if absent, it errors with the exact
  command to run. The web UI shows an "authenticate Claude" affordance that surfaces the same command.

### 2.3 Auth comparison

| Concern              | Codex                                  | Claude Code                                       |
|----------------------|----------------------------------------|---------------------------------------------------|
| Default mechanism    | sync `~/.codex` OAuth session          | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`  |
| Token lifetime       | session-bound (re-sync as needed)      | ~1 year                                           |
| API-key alternative  | `OPENAI_API_KEY`                       | `ANTHROPIC_API_KEY`                               |
| Copy creds dir?      | yes (session material)                 | **no** (keychain/machine-bound)                   |
| Injected as          | staged files + env                     | env var only (cleaner)                            |
| Wiped on destroy     | yes                                    | yes (env + any staged token file)                 |

---

## 3. Configuration mapping (Codex → Claude Code)

`render_config` stages per-backend files into the run directory. The two backends are conceptually
parallel:

| Concept                | Codex                                   | Claude Code                                            |
|------------------------|-----------------------------------------|--------------------------------------------------------|
| Main config            | `~/.codex/config.toml`                  | `.claude/settings.json` (+ `~/.claude/settings.json`)  |
| Model selection        | `model = "..."` in config.toml          | `"model": "..."` in settings.json                      |
| Non-interactive / auto | approval policy in config.toml          | `permissions.defaultMode` + headless flags (§5)        |
| MCP servers            | `[mcp_servers.*]` in config.toml        | `.mcp.json` (`mcpServers`) or `claude mcp add`         |
| Skills                 | `~/.codex/skills/<name>/` + `openai.yaml`| `.claude/skills/<name>/SKILL.md`                       |
| Subagent roles         | `roles/*.toml` (exploit_tester, …)      | `.claude/agents/<name>.md` (frontmatter + prompt)      |

`images/ctf-toolbox/agent-config/` holds both template trees (`codex/`, `claude/`). The existing
skills under `skills/*/` ship a Codex `agents/openai.yaml`; the refactor adds a Claude `SKILL.md` (and
where the skill defines a subagent role, a `.claude/agents/*.md`) so the same capability exists on both
backends. The two existing roles map directly:

- `exploit_tester` → `.claude/agents/exploit-tester.md`
- `docs_researcher` → `.claude/agents/docs-researcher.md`

### 3.1 MCP servers (the GDB + IDA case)
Both backends must expose the bundled **GDB stdio MCP** and the optional **remote IDA HTTP MCP**.
- Codex: `[mcp_servers.gdb]` (stdio) and `[mcp_servers.ida]` (http) in `config.toml`.
- Claude: `.mcp.json`:
  ```json
  {
    "mcpServers": {
      "gdb": { "type": "stdio", "command": "python3", "args": ["/opt/mcp/gdb_mcp.py"] },
      "ida": { "type": "http", "url": "${CTFVM_DEFAULT_IDA_MCP_URL}" }
    }
  }
  ```
`render_config` generates whichever form matches `ctx.agent_backend` from one neutral MCP spec list,
so MCP servers are declared once in the core and rendered per backend.

---

## 4. Launching the main session (`supervisor_launch_cmd`)

`runner/supervisor.sh` becomes backend-agnostic: it calls `supervisor_launch_cmd(ctx)` and runs the
returned command inside the toolbox container in the `ctf:supervisor` tmux window.

- **Codex:** `codex --no-alt-screen <flags> "<initial prompt>"` (today's behavior, multi-agent on).
- **Claude Code:** interactive TTY session in tmux, started non-interactively-safe:
  `claude --permission-mode bypassPermissions <allowed-tools> "<initial prompt>"`
  (`bypassPermissions` is acceptable because this is a disposable, operator-controlled VM — the whole
  point of the sandbox). The initial prompt is the same supervisor instruction text from
  `prompts/supervisor/instructions.txt`.

Both run **inside tmux** so the existing monitor/attach/steer machinery works identically.

---

## 5. Subagents — the hard part, and how parity is achieved

This is where the backends genuinely differ, and where the platform must impose a common model.

### 5.1 The constraint
- **Codex** subagents are *real resumable sessions*: each gets a UUID; `codex resume <uuid>` attaches
  to it. Today `subagent-tmux-bridge.sh` tails the Codex rollout JSONL, detects `thread_spawn` /
  `agent_id`, and opens a `subagent-<uuid>` tmux session running `codex resume <uuid>`.
- **Claude Code** subagents (`.claude/agents/*.md` via the Task tool) are *in-process, non-resumable,
  summary-only*. They **cannot** be attached, resumed, or steered individually. (Agent Teams exist but
  are experimental and don't expose per-teammate session IDs cleanly.)

So we cannot mirror Claude's *native* subagents the way we mirror Codex threads. Instead we **invert
the spawn step** and let the platform own the subagent concept.

### 5.2 The common model
Every subagent, on every backend, becomes: **a session with a known UUID → a `subagent-<uuid>` tmux
session → a JSONL transcript we tail → steerable via the backend's resume command.** The only thing
that differs is how the session is *created* and *detected*.

| Step              | Codex                                              | Claude Code                                                        |
|-------------------|----------------------------------------------------|--------------------------------------------------------------------|
| Spawn             | Supervisor spawns a native thread                  | Platform spawns a **separate headless `claude` session** per hypothesis, with a known `--session-id` UUID |
| Detect            | Tail rollout JSONL for `thread_spawn` + `agent_id` | We already know the UUID (we created it); register it directly     |
| Mirror to tmux    | `subagent-<uuid>` runs `codex resume <uuid>`       | `subagent-<uuid>` tails `~/.claude/projects/<proj>/<uuid>.jsonl` and/or runs an interactive `claude --resume <uuid>` |
| Live transcript   | rollout JSONL                                       | session JSONL (`~/.claude/projects/.../<uuid>.jsonl`)              |
| Steer / follow-up | `codex resume <uuid>` (same thread)                | `claude --resume <uuid>` (same session)                           |
| Stop              | end thread                                          | end session process                                                |

Concretely, `spawn_subagent`:
- **Codex:** instruct the supervisor (via `$ctf-exploit-subagent` skill) to spawn a native subagent;
  the bridge picks it up. (Backend's `detect_subagents` does the work.)
- **Claude:** `uuid=$(uuidgen)`; launch
  `claude -p "<hypothesis>" --session-id "$uuid" --output-format stream-json --permission-mode bypassPermissions <allowed-tools>`
  inside a new `subagent-<uuid>` tmux window; register the `Subagent(uuid, ...)` immediately.

`subagent-tmux-bridge.sh` keeps its Codex path and gains a Claude path; both end at the same place — a
named tmux session per subagent that the UI lists and the operator can steer.

### 5.3 What necessarily diverges (documented, not hidden)
- **Granularity of native subagents.** Claude's *own* Task-tool subagents (spawned by Claude, not by
  us) remain opaque (summary-only) and are surfaced read-only by detecting `Task` tool-use events in
  the transcript. The *steerable* parallel work uses platform-spawned sessions instead. The supervisor
  prompt for the Claude backend is tuned to prefer platform-spawned hypothesis sessions for work the
  operator should be able to watch and steer.
- **Detection source.** Codex emits explicit `thread_spawn`; Claude has no equivalent event, so we rely
  on us-known UUIDs (spawn) plus `Task` tool-use detection (read-only awareness).

This divergence is intrinsic to the two products and is encapsulated entirely inside the two backend
implementations — the UI and CLI see one uniform `list[Subagent]`.

---

## 6. Steering (`steer`)
Uniform at the platform layer: the UI/CLI call `POST .../panes/send` with a tmux target and
text; `steering.py` validates the target and base64-injects the text via `tmux send-keys`. Because both
backends run their (sub)agents in tmux, steering is identical regardless of backend. The backend's
`steer` is only needed for non-tmux follow-up paths (e.g. a headless `claude --resume` turn) and
defaults to the tmux path.

---

## 6b. Frontend feature surface

The frontend must expose the **normal day-to-day features** of whichever agent a run uses — not just
raw pane text — and render them *identically* across backends. To keep the SPA backend-neutral, the
`AgentBackend` translates each backend's native signals into one uniform shape that rides along with
the snapshot:

```python
class AgentBackend(ABC):
    def frontend_features(self, run: RunRecord, snapshot: RawSnapshot) -> AgentFeatures: ...

@dataclass
class AgentFeatures:
    session: SessionInfo | None              # id, resumable, parent
    pending_approval: ApprovalPrompt | None  # detected tool/permission prompt + choices
    model: str                               # active model
    mode: str                                # approval/permission mode
    mcp_servers: list[McpStatus]             # name, transport, enabled/healthy
    skills: list[SkillStatus]                # name, available
    plan: list[PlanItem]                     # plan/todo, when emitted
    diffs: list[FileDiff]                    # pending/applied edits, when emitted
    usage: UsageInfo | None                  # tokens/cost, turn state (mid-turn/waiting/idle)
    capabilities: set[str]                   # which of the above this backend actually supports
```

`capabilities` drives graceful degradation: the SPA shows a control only if the backend lists it, so a
feature one product lacks simply doesn't render (no errors, no empty widgets).

### Backend → feature mapping

| UI feature | Codex source | Claude Code source |
|------------|--------------|--------------------|
| Pending approval | approval prompt text in the supervisor pane → Approve/Always/Deny keys | permission prompt in pane, or `can_use_tool`/permission events in stream-json |
| One-click trust | generalizes today's `trust` (send `1`+Enter) | same control → maps to the permission accept choice |
| Session id + resume | rollout JSONL session/thread id → `codex resume` | `session_id` from `--output-format json` → `claude --resume` |
| Subagents | native `thread_spawn` (§5) | platform-spawned sessions (§5) |
| MCP servers | `config.toml [mcp_servers.*]` | `.mcp.json` `mcpServers` |
| Skills | `~/.codex/skills/*` | `.claude/skills/*` |
| Model / mode | config.toml model + approval policy | settings.json `model` + `permissions.defaultMode` |
| Plan / todo | plan items in transcript, when present | `TodoWrite`/plan tool events in stream-json |
| Diffs / edits | apply-patch tool calls | `Edit`/`Write` tool_use events in stream-json |
| Usage / turn state | token usage lines in transcript | `usage` in stream-json result/message events |

The detection lives entirely in `agents/codex.py` / `agents/claude_code.py`; `snapshot.py` carries
`AgentFeatures` through, the API serializes it (additive to the run snapshot in [`API.md`](API.md)), and
the SPA renders one set of controls. New backends get the same UI for free by filling in this mapping.

---

## 7. Skills inventory (must exist on both backends)
The repo's skills define CTF capabilities; each needs a Claude equivalent so behavior is backend-neutral:

| Skill                    | Codex artifact            | Claude artifact to add                         |
|--------------------------|---------------------------|------------------------------------------------|
| `ctf-exploit-subagent`   | `agents/openai.yaml`      | `SKILL.md` + `.claude/agents/exploit-tester.md`|
| `ctf-docs-subagent`      | `agents/openai.yaml`      | `SKILL.md` + `.claude/agents/docs-researcher.md`|
| `gdb-mcp`                | `agents/openai.yaml`      | `SKILL.md` (MCP server shared via `.mcp.json`) |
| `webhook-site-callbacks` | `agents/openai.yaml`      | `SKILL.md`                                      |

`clanker sync-skill` stages the correct artifact set for the run's backend.

---

## 8. Open questions to confirm during implementation
1. Exact Claude headless flags on the pinned CLI version: permission-mode spelling, whether
   `--session-id` is accepted for new sessions (fallback: capture `session_id` from the first
   `--output-format json` response and resume by it).
2. Whether tailing a Claude subagent's JSONL gives a good enough live pane, or whether we additionally
   run an interactive `claude --resume <uuid>` in the tmux pane for a richer view.
3. Codex API-key mode parity (some Codex features assume OAuth session) — confirm `OPENAI_API_KEY`
   alone is sufficient for the supervisor + subagents.
