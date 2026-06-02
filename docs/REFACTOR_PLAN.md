# Refactor Plan

Staged migration to the architecture in [`ARCHITECTURE.md`](ARCHITECTURE.md), the agent layer in
[`AGENTS.md`](AGENTS.md), the API in [`API.md`](API.md), fixing the verified bugs in
[`BUGS.md`](BUGS.md). Work happens on `refactor/platform-v2`.

---

## Guiding principles

- **No regressions on day-1 commands.** `start`, `attach`, `shell`, `vscode`, `destroy`, `inject`,
  `sync-down`, `sync-up`, `logs`, `fetch`, `send`, `key`, `status`, `ui` keep working throughout.
- **Strangler migration.** The bash `scripts/ctfvm` entrypoint keeps working while commands are ported
  to the Python core one at a time. We never have a flag-day rewrite.
- **Control plane is the only hot path.** Monitoring/steering/artifacts never fall back to SSH; SSH is
  break-glass only (Invariant 1).
- **One source of truth per concern.** Models in `models.py`, state in `state.py`, routes are thin.
- **Both agent backends stay green.** Every phase that touches the agent path is validated on *both*
  Codex and Claude Code before it's considered done.
- **Smallest blast radius per phase.** Each phase is independently shippable and testable.

---

## Feature inventory — keep / cut / defer (recommended)

You asked me to recommend the cuts. Grounded in the code map:

| Feature / surface | Recommendation | Why |
|-------------------|----------------|-----|
| `start`, `destroy`, `status`, `attach`, `shell`, `vscode`, `logs`, `inject`, `send`, `key`, `sync-down`, `sync-up`, `sync-skill`, `fetch`, `ui`, `image`, `cleanup-state`, `mcp` | **KEEP** | Core lifecycle, steering, and ops. |
| WireGuard VPN (`vpn`, auto-start) | **KEEP** | Primary local-network reachability path. |
| `chat` (supervisor-only attach) | **KEEP (fold)** | Cheap; becomes `attach --window supervisor`. |
| `ctfvm bridge` (legacy TCP bridges) | **CUT** | Superseded by the VPN; already labeled "legacy" in `CTFVM.md`; ~130 lines of tangled NAT/firewall code (`cmd_bridge_up`). VPN covers TCP+UDP. |
| `ctfvm ideas` + `runner/spawn-idea-workers.sh` | **CUT** | Superseded by supervisor-native subagents (the model we're standardizing in AGENTS.md §5). Removing it removes a parallel, divergent spawn path. |
| `POST /api/select-directory` (macOS Finder picker) | **CUT** | macOS-only, not portable, breaks the "runs from web UI" story; replaced by a typed path / web file field (B3 neighbor). |
| `GET /artifacts` route + `render_artifacts_page()` | **CUT** | Dead code (B3) — route calls the wrong renderer; page unreachable. |
| `ctfvm monitor` (terminal poll loop) | **DEFER/merge** | Overlaps `attach` + `logs` + the web UI. Keep a thin shim during migration; retire once the UI covers it. |
| All non-versioned `/api/*` JSON routes | **CUT → replace** | Replaced by `/api/v1/*` (see API.md removed-endpoints table). |
| SSH fallback inside snapshot/steering/artifacts | **CUT** | Violates Invariant 1; replaced by control-client-only with a typed error (B7). |
| Embedded HTML-in-Python frontend | **CUT → extract** | Moves to `clanker/server/frontend/index.html` (B12). |

Net effect: fewer commands, one spawn/subagent model, one steering path, a portable UI, and no dead
endpoints — while every *capability* an operator actually uses is preserved.

---

## Phase 0 — Branch, docs, baseline ✅ (this phase)

- [x] Branch `refactor/platform-v2`.
- [x] `ARCHITECTURE.md`, `AGENTS.md`, `API.md`, `BUGS.md`, `REFACTOR_PLAN.md` (grounded in real code).
- [x] `SMOKE_TEST.md` — manual end-to-end checklist (start → monitor → steer → subagent → artifact →
      destroy) run for **each** provider × **each** agent backend.

## Phase 1 — Python core foundation (no behavior change)

Stand up `clanker/` and make it the home of shared logic, without removing the bash entrypoint.

- `models.py` — all dataclasses (incl. `agent_backend` on `RunRecord`, `Subagent`).
- `state.py` — `RunRegistry` (load/merge/persist, **single `RLock`** → fixes B4; reads old state files,
  defaults `agent_backend="codex"`).
- `controlclient.py` — typed control-plane client with connect+read timeouts and typed errors (B8).
- `config.py` — settings precedence (CLI > `.env` > cloud env > `.ctfvm/config.json` > defaults).
- Acceptance: `RunRegistry` lists exactly what the bash CLI lists from the same `.ctfvm/`; control
  client round-trips exec/upload/download against a live VM.

## Phase 2 — CloudProvider abstraction + first ported commands ✅

Done:
- [x] `clanker/providers/`: `CloudProvider` ABC (`base.py`) + `gcp.py`, `digitalocean.py`
      (discover + status ported from legacy `ctfvm_ui.providers`); `CloudProviderRegistry`
      and `build_run_registry()` wire live discovery/status into `RunRegistry`. Provisioning +
      break-glass declared on the ABC but raise `NotImplementedError` until Phase 3.
- [x] `clanker/commands.py`: ported `status`, `cleanup-state`, `fetch`, `sync-down` onto the core
      (control client for transfer; `RunRegistry.load_raw` for status extras).
- [x] Bash strangler: `ctfvm cleanup-state` now delegates to `python -m clanker cleanup-state`
      (verified end-to-end; flag interface unchanged). `status`/`fetch`/`sync-down` are available
      via `python -m clanker` and unit-tested; their **bash cutover is deferred to Phase 3**, when
      the selector/`--pick` resolution is ported (today that logic lives in bash).
- [x] `tests/test_commands.py`: cleanup-state (remove/keep/prune/cli-missing/dry-run/promote),
      status output, and provider-registry dispatch — all via a fake provider (no live cloud).

Deferred (with reason):
- `logs` stays bash: it is a streaming `tail -f`, which does not map cleanly onto the
  request/response control plane (the legacy control-plane path blocks until timeout). It moves
  when steering/streaming is reworked in Phase 4.
- SSH fallback for `fetch`/`sync-down` is intentionally dropped in the port (control-plane only,
  clear error otherwise) per Invariant 1 / B7; the default flow always has a control plane.
- `start`/`destroy` remain bash and untouched.

Acceptance: ported command bodies behave identically; `cleanup-state` cut over and green;
`start`/`destroy` still bash. ✅

## Phase 3 — Agent backend abstraction (Codex parity first, then Claude)

### 3a — Agent layer core ✅ (config / auth / launch — pure, fully tested)
- [x] `clanker/agents/base.py`: `AgentBackend` ABC + neutral specs (`McpServer`, `SubagentRole`,
      `AgentConfigSpec`, `StagedFile`, `AuthMaterial`) + `default_agent_spec` (bundled GDB MCP, the
      two standard roles, IDA via `CTFVM_DEFAULT_IDA_MCP_URL`). VM-runtime methods declared, raise
      `NotImplementedError` until 3b.
- [x] `clanker/agents/codex.py`: reproduces today's `config.toml` + `roles/*.toml` (validated: the
      rendered TOML parses and matches the committed config), `~/.codex` session sync (with
      `OPENAI_API_KEY` alternative), and the exact `codex --no-alt-screen --enable multi_agent
      --ask-for-approval never --sandbox danger-full-access` launch.
- [x] `clanker/agents/claude_code.py`: `settings.json` (`permissions.defaultMode=bypassPermissions`)
      + `.mcp.json` (stdio + http) + `.claude/agents/*.md`; auth via `CLAUDE_CODE_OAUTH_TOKEN`
      (or `ANTHROPIC_API_KEY`), never copying credential files; `claude --permission-mode
      bypassPermissions [--model …]` launch.
- [x] `build_agent_backend()` factory + `clanker render-agent-config --agent …` introspection cmd.
- [x] `tests/test_agents.py` (12 tests): codex/claude render, IDA-via-env, launch cmds, auth
      materialization (session sync / API key / missing-creds), registry normalization.
- Intentional cleanup: dropped the legacy hardcoded `idaPro` URL (a dev-leftover droplet IP); IDA is
  wired only via `CTFVM_DEFAULT_IDA_MCP_URL`.

### 3b — Agent ↔ provisioning glue ✅ (the testable, backward-compatible parts)
- [x] `clanker/secretstore.py` + `Settings` read of `.ctfvm/secrets.json` (0600, gitignored).
- [x] `clanker auth claude [--token]` (wraps `claude setup-token`, or stores a pasted token) +
      `clanker auth show`. `materialize_auth` picks the token up automatically.
- [x] `clanker stage-agent --agent … --staging-dir …`: materializes the **full** agent payload —
      rendered config/MCP/role files, copied local auth (Codex session), and the `agent/` control
      files consumed by `supervisor.sh` (`backend`, `launch.cmd`, `container.env`, `wipe-paths.txt`).
      Refuses with `AUTH_REQUIRED` (rc 3) when Claude creds are missing.
- [x] `runner/supervisor.sh` is now **backend-aware**: it reads `agent/{backend,launch.cmd,
      container.env}` and launches whatever the backend staged, passing per-agent container env to
      `docker exec`. When `agent/` is absent (older runs) it falls back to the exact historical Codex
      defaults — existing runs are unaffected. (bash syntax + parsing logic verified in isolation.)
- [x] `tests/test_auth_stage.py` (4 tests): secret store roundtrip + 0600; codex/claude stage payload;
      Claude-no-creds → `AUTH_REQUIRED`. 30 tests total green.

### 3b-cont — `start`/`destroy` wiring (remaining; needs a live VM to validate)
- `cmd_start`: accept `--agent`; replace the Codex-specific config upload + `~/.codex` sync with a call
  to `clanker stage-agent` (one unified path for both backends) and upload the staged tree; refuse on
  `AUTH_REQUIRED`. `cmd_destroy`: shred the backend's `wipe-paths.txt` (already shreds `.codex`/`.claude`
  generically — extend to read the staged list).
- `subagent-tmux-bridge.sh` backend-aware (Codex native threads; Claude platform-spawned sessions —
  AGENTS.md §5). Port `inject`/`send`/`key`/`sync-skill`; `provisioning.py`/`vpn.py`/`images.py`.
- Acceptance (live): a Codex run via the new path matches today's behavior; a Claude run starts,
  authenticates via the staged token, and is monitorable.

## Phase 4 — Snapshot/steering/artifacts rewrite (bug fixes B1, B7)

- `snapshot.py` — single JSON envelope, base64 fields (kills B1 marker collision).
- `steering.py`, `artifacts.py` — preserve existing (correct) validation (R1/R2); control-client only,
  typed error on missing creds (B7). SSH limited to `CloudProvider.ssh_break_glass`.
- Acceptance: snapshots correct even when pane/findings content contains marker strings and `|`;
  steering works for text with `|`, backticks, `$`; legacy runs without creds return a clean `error`.

## Phase 5 — Local server v1 + extracted SPA (B3, B12)

- `server/app.py` + `routes.py` implementing `/api/v1/*` per API.md; `{ok,data}` / `{ok,error,code}`.
- Extract the SPA to `frontend/index.html`; delete dead `/artifacts` route + `render_artifacts_page`.
- Remove `select-directory`; SPA uses a typed challenge-path field with `agent_backend` selector.
- Old non-versioned routes deleted (return 404).
- **Agent-feature parity in the frontend (backend-neutral).** The SPA must surface the *normal*
  day-to-day features of whichever agent a run uses, not just raw pane text. The agent backend exposes
  these as uniform snapshot/endpoint data; the SPA renders them the same way for Codex and Claude Code:
  - **Approvals / permission prompts** — detect a pending tool/command approval in the pane and offer
    one-click Approve / Approve-always / Deny (generalizes today's `trust` button). Codex approval
    prompts and Claude permission prompts both map to this control.
  - **Sessions** — show the current session/thread id; resume/continue a prior session
    (`codex resume` / `claude --resume`) from the UI.
  - **Subagents** — the `/subagents` list with per-subagent live pane, status, and steering (AGENTS.md §5).
  - **MCP servers & skills** — list configured MCP servers (gdb, ida, …) and available skills for the
    run, with health/enabled state; this is read from the rendered agent config.
  - **Model & mode** — display the active model and permission/approval mode; allow switching where the
    backend supports it.
  - **Plan / todo & diffs** — surface the agent's plan/todo and file diffs/edits when the backend emits
    them (Claude stream-json `Task`/tool events; Codex equivalents).
  - **Token/cost & turn status** — show usage and whether the agent is mid-turn, waiting, or idle.
  Each item degrades gracefully: if a backend doesn't expose a feature, the control hides itself rather
  than erroring. The mapping of these to each backend's signals lives in `agents/<backend>.py` and is
  documented in AGENTS.md (new §"Frontend feature surface").
- Acceptance: SPA drives fleet + focused + subagents + artifacts + steering + approvals + sessions
  against `/api/v1/`; all removed routes 404; the same UI controls work on a Codex run and a Claude run.

## Phase 6 — Claude Code backend + VM hardening

- `agents/claude_code.py` — `materialize_auth` (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`,
  configurable `ANTHROPIC_API_KEY`); `render_config` (settings.json + `.mcp.json` + `.claude/agents`,
  `.claude/skills`); `supervisor_launch_cmd`; platform-spawned subagent sessions (AGENTS.md §5.2).
- `clanker auth claude` command + UI/`AUTH_REQUIRED` affordance.
- Claude `SKILL.md` + `.claude/agents/*.md` equivalents for all four skills (AGENTS.md §7).
- Toolbox Dockerfile installs the Claude CLI alongside Codex; `startup.sh` install error handling (B11).
- VM hardening: control-server request logging (B5) + `/exec` body cap; `flock` on findings/log
  appends (B6); `asyncio.Lock` in `gdb_mcp.py` (B2).
- Acceptance: a full **Claude Code** run starts, is monitored, has a steerable subagent in its own
  tmux session, and produces artifacts — feature-parity with Codex on the same challenge.

## Phase 7 — Retire bash entrypoint, polish

- Final commands ported; `scripts/ctfvm` becomes a one-line shim to `python -m clanker` (or removed
  with a deprecation note). Cut `bridge`, `ideas`, `monitor` per the inventory.
- Update `CTFVM.md` / `HTTP_CONTROL_PLANE_ARCHITECTURE.md` to the new surface; fix stale absolute paths
  (e.g. `/Users/0c34n/...` links in `CTFVM.md`).

---

## Out of scope (non-goals for this branch)

Other AI providers beyond Codex/Claude (gemini, opencode), other clouds (Incus/local/VPS), Terraform
static infra, multi-user UI auth, hosted aggregator/load balancer, TLS + rate limiting on the control
plane. These become easy follow-ons *because* of the two ABCs, but are not in this branch.

---

## Acceptance criteria (whole refactor)

1. `SMOKE_TEST.md` passes for **{gcp, digitalocean} × {codex, claude-code}**.
2. All verified bugs B1–B12 fixed; refuted R1–R3 protections still present (tests/asserts).
3. All removed endpoints return 404; `/api/v1/*` matches API.md schemas.
4. A subagent is visible and steerable in its own tmux session on **both** backends.
5. Steering handles text containing `|`, backticks, `$`; snapshots survive marker strings in content.
6. The bash CLI is a shim (or removed); no business logic lives outside `clanker/` and the VM scripts.
7. `clanker auth claude` materializes a token; a Claude run authenticates without copying credentials.
