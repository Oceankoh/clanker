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

## Phase 4 — Snapshot/steering/artifacts rewrite (bug fixes B1, B7) ✅

- [x] `clanker/snapshot.py` — the remote gatherer is now a **single Python program** (shipped via
      `remote.py::remote_python`, base64-wrapped, run as `ctf`) that emits **one JSON object** with
      every variable-length field base64-encoded. No markers, so no collision (B1). Ported
      `derive_challenge_state` (solved/blocked/stopped/halted/stalled/progressing) + the activity/HALTED
      logic. `fetch_snapshot` is **control-plane only** — `ControlPlaneError` becomes the snapshot
      `error` field, never an SSH fallback (B7 / Invariant 1).
- [x] `clanker/steering.py` — `send_text`/`send_keys`/`trust_prompt`/`set`/`clear` status: validate the
      target/keys **locally before any remote call** (R2), base64 the text (no shell quoting of user
      content), control-client only. `SteeringError` for bad input → 400.
- [x] `clanker/artifacts.py` — `preview` (capped, JSON envelope) / `download` (control-plane
      `/files/download`) / `build_bundle`; `sanitize_relpath` (R1) before any remote call.
- [x] `clanker/validation.py` — `sanitize_relpath` / `safe_target` / `KEY_RE` ported verbatim
      (preserved, not introduced — BUGS.md R1/R2).
- [x] Tests (16): **B1 proof** — pane output + findings containing `__FINDINGS__`/`__SUPERVISOR__`/
      `__PANE_END__`/`a|b|c` round-trip exactly; the gatherer script compiles and runs locally (no-tmux
      branch emits valid JSON); steering rejects `ctf:supervisor;rm -rf /` without contacting the VM and
      base64s `| \` $ ;` text; artifacts reject `artifacts/../../etc/passwd`. **50 tests total green.**

Wiring these modules into the HTTP server is Phase 5; the bash CLI's monitor/snapshot path is replaced
when the server lands.

## Phase 5 — Local server v1 + extracted SPA (B3, B12) ✅

Done:
- [x] `clanker/server/`: `app.py` (stdlib router, `{ok,data}`/`{ok,error,code}` envelope, all
      `/api/v1/*` routes from API.md), `service.py` (`UiService` over RunRegistry + providers + control
      client + snapshot/steering/artifacts, typed `ApiError`), `serialize.py` (model→JSON), `jobs.py`
      (bounded thread-safe `SpawnJobTracker` with run-id detection + eviction).
- [x] Extracted SPA at `server/frontend/index.html` (no more HTML-in-Python — B12); fleet + focused
      (panes/findings/supervisor/artifacts/subagents), steering box, Ctrl-C/Trust, solved/blocked/clear,
      artifact preview/download/bundle. `clanker serve` boots it.
- [x] Dead `/artifacts` route + `render_artifacts_page` gone (B3 — the new server simply doesn't have
      them); `select-directory` removed (SPA uses a typed path); all non-versioned routes → 404.
- [x] Subagents are derived live from the snapshot (`subagent-*` tmux sessions) — uniform across backends.
- [x] `tests/test_server.py` (12): in-process server + fake control client — health, runs list,
      snapshot (B1 marker content survives through the real parser), subagents, steering 400 on bad
      target + 200 on special-char text, status set/clear, artifact preview/download (read as ctf),
      bundle, removed-routes 404, SPA served; plus job-tracker run-id detection + limit. **63 tests total.**

### 5a-operational — UI redesign ✅ (the testable wins)
Addresses the concrete UX problems in the first SPA cut:
- [x] **Steer vs queue** — new `POST /api/v1/runs/{id}/inject` appends to `inject.queue` (the safe path
      the tmux bridge feeds at a good point). The SPA defaults to **Queue (inject)** and offers explicit
      **Send now (steer)** for typing into the live pane.
- [x] **Enter-to-send** — the steer box is a `textarea`: Enter submits (in the selected mode),
      Shift+Enter inserts a newline.
- [x] **Quick actions** — Ctrl-C, Trust (1↵), Approve (y), Deny (n), Esc as labeled key-sends (no
      fragile auto-detection).
- [x] **Artifacts** — images render inline, binaries get a client-side hex dump (preview now returns the
      capped bytes as `b64`), text gets a copy button; panes auto-scroll to the latest output.
- [x] Tests: inject endpoint + `steering.inject_message`, binary-preview `b64`, SPA element markers.
      88 tests total.

### 5b — Chat-transcript experience ✅ (built + validated against real transcripts)
The "full Claude/Codex web UI" piece — a structured chat transcript instead of a raw terminal scrape.
Turned out to be testable after all: the parser only needs realistic session JSONL, and real Codex/
Claude transcripts on the dev machine provided ground truth.
- [x] `clanker/transcript.py`: `parse_codex_rollout` (response_item: message/reasoning/function_call/
      _output, custom_tool_call) + `parse_claude_session` (content blocks: text/thinking/tool_use/
      tool_result) -> uniform `TranscriptEvent` (role, kind, text, tool, tool_input, is_error).
      **Validated on real data**: 0 malformed events across 800+ events, correct tool names
      (exec_command/apply_patch, Agent/Bash), paired tool_call/tool_result.
- [x] `AgentBackend.transcript_script(remote_run_dir)`: per-backend remote Python that finds the newest
      session JSONL (codex `.codex/sessions/rollout-*.jsonl`, claude `.claude/projects/**/*.jsonl`) and
      tails it. End-to-end run of the *actual* gatherer over real files produced 110 clean events.
- [x] `GET /api/v1/runs/{id}/transcript` + `UiService.transcript` (control-plane only).
- [x] SPA **Transcript** tab (now the default focus view): role-styled chat bubbles, dim reasoning,
      tool calls with args, collapsible/erroring results; near-bottom auto-follow.
- [x] Tests: parser fixtures (codex + claude), backend dispatch, endpoint via fake client. 92 total.
- Only the control-plane transport (same tested urllib client) and the exact on-VM active-session path
  are unexercised until a live run — everything else is verified.

Original 5b spec (the AgentFeatures surface):
- **Agent-feature parity in the frontend (backend-neutral).** The SPA must surface the *normal*
  day-to-day features of whichever agent a run uses, not just raw pane text. The agent backend exposes
  these as uniform snapshot/endpoint data; the SPA renders them the same way for Codex and Claude Code:
  - **Approvals / permission prompts** — detect a pending tool/command approval in the pane and offer
    one-click Approve / Approve-always / Deny (generalizes today's `trust` button). Codex approval
    prompts and Claude permission prompts both map to this control.
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

## Phase 6 — Claude Code backend + VM hardening ✅ (testable parts)

- [x] `agents/claude_code.py` + `clanker auth claude` + `AUTH_REQUIRED` — landed in Phases 3a/3b.
- [x] VM hardening (B5): `vm/control_server.py` structured per-request stderr logging + separate 10 MB
      `/exec` body cap (`MAX_EXEC_BYTES`). Tested in `tests/test_control_server.py` (auth, exec
      round-trip, 413 cap).
- [x] VM hardening (B2): `gdb_mcp.py` `_TOOL_LOCK` serializes stateful tools; `interrupt` bypasses it.
- [x] VM hardening (B11): `startup.sh` now errors if **both** codex installs fail, and installs the
      Claude CLI (`@anthropic-ai/claude-code`) best-effort alongside Codex (the container picks both up
      via the `/opt/ctfvm/npm-global` bind mount — no Dockerfile change needed).
- [x] Skills: the repo's `SKILL.md` files are already Claude-compatible frontmatter; the subagent roles
      (`.claude/agents/*.md`) are generated by the agent layer — so skills work on both backends. The
      Codex-specific wording in skill bodies ("Codex session") is cosmetic.
- B6 re-investigated and **downgraded** — the `findings.md`/`supervisor.log` writes we control are not
  contended (single supervisor thread; bridge logs a separate file). The real concurrent writer is the
  agent itself; mitigated by the subagent-evidence-dir convention. See BUGS.md B6.
- Acceptance (live): a full Claude Code run starts, authenticates via the staged token, and is
  monitorable — deferred to a VM smoke run (with the 3b-cont `start` wiring).
- Acceptance: a full **Claude Code** run starts, is monitored, has a steerable subagent in its own
  tmux session, and produces artifacts — feature-parity with Codex on the same challenge.

## Phase 7 — Polish & docs (✅ docs); bash retirement bundled with the live port

Done (safe, non-live):
- [x] Top-level `CLAUDE.md` orienting the repo (layout, the two ABCs, conventions, status).
- [x] `CTFVM.md`: fixed the stale `/Users/0c34n/...` absolute links; added a "Python core (`clanker`)"
      section listing the new `python -m clanker` commands (`serve`/`runs`/`status`/`auth`/`stage-agent`
      /…) and pointing at ARCHITECTURE/AGENTS/API.

Bundled into the live 3b-cont port (NOT done blind on the legacy bash file):
- Cutting `bridge` (legacy TCP) and `ideas` + `spawn-idea-workers.sh` (superseded by subagents), and
  turning `scripts/ctfvm` into a shim, are entangled with `cmd_start`'s upload list / validation in the
  4.2k-line bash CLI. Doing them without a VM to re-verify `start` would be reckless, so they execute
  **together with** the `start`/`destroy` Python port (3b-cont), when the bash file is retired wholesale.
  The keep/cut decisions are already recorded in the inventory table above.

---

## Out of scope (non-goals for this branch)

Other AI providers beyond Codex/Claude (gemini, opencode), other clouds (Incus/local/VPS), Terraform
static infra, multi-user UI auth, hosted aggregator/load balancer, TLS + rate limiting on the control
plane. These become easy follow-ons *because* of the two ABCs, but are not in this branch.

---

## Acceptance criteria (whole refactor)

Local / unit-verifiable — **met** (69 tests green):
2. ✅ Verified bugs fixed (B1,B2,B4,B5,B7,B8,B10,B11,B12; B3 by removal; B6 downgraded with evidence;
   B9 core done). Refuted R1–R3 protections preserved with asserts.
3. ✅ Removed endpoints return 404; `/api/v1/*` produces the API.md shapes (in-process server tests).
5. ✅ Steering handles `|`/backticks/`$` (base64 transport); snapshots survive marker strings (B1 proof).
7. ✅ `clanker auth claude` stores a token; the Claude backend authenticates via env injection, never
   by copying credential files (stage-agent + auth tests).

Live / VM-gated — **remaining** (need a real run to validate):
1. `SMOKE_TEST.md` for **{gcp, digitalocean} × {codex, claude-code}**.
4. A subagent visible + steerable in its own tmux session on **both** backends end-to-end.
6. `start`/`destroy` ported so the bash CLI becomes a shim (3b-cont).

These depend on provisioning a VM (gcloud/doctl creds + a challenge), which is outside this
environment; everything they build on is unit-tested.
