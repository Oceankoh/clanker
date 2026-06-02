# Smoke Test — End-to-End Checklist

Manual acceptance for the refactor. A run is **green** only when the full lifecycle below passes for
the target cell of the matrix. This doubles as a regression guard: several steps deliberately exercise
the bugs in [`BUGS.md`](BUGS.md).

Companion docs: [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`AGENTS.md`](AGENTS.md) · [`API.md`](API.md) ·
[`REFACTOR_PLAN.md`](REFACTOR_PLAN.md).

> Commands use the **target** entrypoint `clanker …` and the `--agent` flag. While the bash entrypoint
> is still in place, substitute `./scripts/ctfvm …` and skip flags it doesn't yet accept — see
> [§9 Running against the current build](#9-running-against-the-current-build).

---

## 1. Coverage matrix

| | **Codex** | **Claude Code** |
|----------------|-----------|-----------------|
| **GCP**        | ☐ run A   | ☐ run B         |
| **DigitalOcean** | ☐ run C | ☐ run D         |

- **Per-commit minimum:** one cell that touches each axis — e.g. **A (gcp+codex)** and **D (do+claude)**.
- **Per-phase / release:** all four cells.
- Each cell is one full pass of §4–§7. Record results in [§10 Sign-off](#10-sign-off).

---

## 2. Prerequisites (once per machine)

- ☐ Cloud auth: `gcloud auth login` (GCP) and/or `doctl auth init` (DigitalOcean).
- ☐ Default project/zone (GCP) or region (DO) set, or passed via flags / `.env`.
- ☐ Toolbox image pushed for the provider under test:
  `clanker image push-registry --provider <gcp|digitalocean>` (or `--use-local-image` per run).
- ☐ **Codex** auth: `codex login` complete locally.
- ☐ **Claude Code** auth materialized locally (no credential copying):
  `clanker auth claude` → runs `claude setup-token`, stores the 1-year token. (AGENTS.md §2.2)
- ☐ A small, known-solvable **practice challenge** dir, e.g. `./.ctf-work/smoke/` containing a trivial
  binary or script. (A challenge that solves in <2 min keeps the loop fast.)

Set per-cell variables to keep the steps copy-pasteable:

```bash
export SMOKE_PROVIDER=gcp          # or digitalocean
export SMOKE_AGENT=claude-code     # or codex
export SMOKE_DIR=./.ctf-work/smoke
export SMOKE_DESC="smoke test"
```

---

## 3. What "pass" means

Every step has an **expected** result. If any expected result is wrong, the cell **fails** — note the
step ID and the deviation in §10. Do not mark a cell green with known failures.

---

## 4. Provision

- ☐ **4.1 Start.**
  ```bash
  clanker start --provider "$SMOKE_PROVIDER" --agent "$SMOKE_AGENT" \
    --dir "$SMOKE_DIR" --desc "$SMOKE_DESC"
  ```
  **Expected:** instance created; CLI polls the control plane and reports ready; a `run_id` is printed.
- ☐ **4.2 State file.** `.ctfvm/<run_id>.json` exists and contains `"agent_backend": "<SMOKE_AGENT>"`
  plus `control_host/port/user/password` and `ip`. (ARCHITECTURE §8)
- ☐ **4.3 Health.** Control plane answers:
  `GET http://<control_host>:<port>/healthz` → `200` with `ok:true`. (no auth)
- ☐ **4.4 Auth landed.** The agent is authenticated on the VM with **no** repeating trust/login prompt
  in the supervisor pane (§5.2 confirms). For Claude: token injected as `CLAUDE_CODE_OAUTH_TOKEN`, not
  a copied `~/.claude`. For Codex: session synced (or `OPENAI_API_KEY` set if configured).
- ☐ **4.5 No auth → clear error.** (Claude only) Temporarily unset the stored token and
  `clanker start --agent claude-code …` **fails fast** with code `AUTH_REQUIRED` and the exact
  `clanker auth claude` remediation. Restore the token after. (API.md, AGENTS.md §2.2)

## 5. Monitor

- ☐ **5.1 UI.** `clanker ui` → open `http://127.0.0.1:8765`. The run appears in the fleet view with
  the correct provider + agent backend and a live `runtime_status`.
- ☐ **5.2 Snapshot.** `GET /api/v1/runs/<run_id>` returns `ok:true` with non-empty `panes` (incl.
  `ctf:supervisor`), `supervisor_tail`, and `metrics`. `challenge_state.state` is one of
  `progressing|stalled|halted|solved|blocked`. (API.md)
- ☐ **5.3 Snapshot survives marker/pipe content (B1 regression).** Inject content that would have
  broken the old marker parser, then re-snapshot:
  ```bash
  clanker send --run <run_id> --target ctf:supervisor \
    --text 'echo "__FINDINGS__ __SUPERVISOR__ a|b|c __PANE_END__"' --enter
  ```
  **Expected:** the snapshot still parses; pane output shows the literal string; **no** truncated or
  cross-wired fields. (Single JSON envelope, ARCHITECTURE §6 / Invariant 4)
- ☐ **5.4 Control-plane only (B7).** Confirm the snapshot/monitor path used the control plane, not SSH:
  with valid control creds it succeeds; with creds removed it returns a clean `error` field (partial
  snapshot, `ok:true`) rather than silently SSHing. SSH is reachable **only** via break-glass (§8).

## 6. Steer & subagents

- ☐ **6.1 Steer with shell-special text (R2/B7 regression).** Send text containing `|`, backticks, `$`,
  `;`:
  ```bash
  clanker send --run <run_id> --target ctf:supervisor \
    --text 'note: a|b `c` $HOME ; ok' --enter
  ```
  **Expected:** the supervisor pane receives the bytes **literally** (base64 transport); no shell
  evaluation, no injection, no error.
- ☐ **6.2 Invalid tmux target rejected locally (R2).** `POST …/panes/send` with
  `target = "ctf:supervisor;rm -rf /"` → `400 BAD_REQUEST`, and the VM is **never** contacted.
- ☐ **6.3 Keys & trust.** `…/panes/keys {keys:"C-c"}` interrupts; `…/panes/trust` accepts a trust
  prompt. Both return `ok:true`.
- ☐ **6.4 Inject.** `clanker inject --run <run_id> --msg "try the off-by-one path"` lands in the run's
  inject queue and is picked up by the supervisor.
- ☐ **6.5 Subagent visible in its own tmux session — BOTH backends (AGENTS.md §5).** Trigger a
  subagent (Codex: supervisor spawns a native thread via `$ctf-exploit-subagent`; Claude: platform
  spawns a headless hypothesis session). Then:
  ```bash
  curl -s http://127.0.0.1:8765/api/v1/runs/<run_id>/subagents | jq
  ```
  **Expected:** at least one entry with a `tmux_target` like `subagent-<uuid>:0`, `state:"running"`,
  `steerable:true`. `clanker attach` shows the `subagent-<uuid>` session alongside `ctf`.
- ☐ **6.6 Steer the subagent.** `…/panes/send` to the subagent's `tmux_target` delivers text to that
  subagent's pane. **This must work identically on Codex and Claude Code.**

## 7. Artifacts, status, break-glass, teardown

- ☐ **7.1 Produce an artifact.** Have the run (or you, via `shell`) write
  `/home/ctf/run/artifacts/exploit.py`.
- ☐ **7.2 List.** Snapshot `artifacts[]` includes `artifacts/exploit.py` with `size_bytes` + `mtime`.
- ☐ **7.3 Preview.** `GET …/artifacts/artifacts%2Fexploit.py` returns the bytes with a sensible
  `Content-Type`. Try a text, a binary, and a >1 MB file.
- ☐ **7.4 Download + bundle.** `…/artifacts/<path>/download` (attachment) and `…/bundle` (`.tar.gz` of
  findings + artifacts + supervisor.log) both succeed; bundle over the cap returns `413`.
- ☐ **7.5 Path traversal rejected (R1 regression).** `GET …/artifacts/..%2f..%2fetc%2fpasswd` →
  `INVALID_PATH`; VM never contacted.
- ☐ **7.6 Status markers.** `POST …/status {state:"solved", note:"flag{...}"}` sets explicit status;
  `DELETE …/status` clears it and reverts to auto detection.
- ☐ **7.7 Break-glass SSH still works.** `clanker shell --run <run_id>` opens an interactive shell;
  `clanker attach` opens tmux. (These are the *only* SSH paths — Invariant 1.)
- ☐ **7.8 Destroy.** `clanker destroy --run <run_id>` → instance deleted on the provider; agent
  credentials wiped on the VM; `.ctfvm/<run_id>.json` and aliases removed. `clanker status` no longer
  lists it. `clanker cleanup-state` reports nothing orphaned.

## 8. VM hardening checks (run once per provider, any agent)

- ☐ **8.1 Request logging (B5).** Control-server journal shows one structured line per request
  (`<ts> <method> <path> <status> <ms>`). (Was a no-op before.)
- ☐ **8.2 `/exec` body cap.** A `>10 MB` `/exec` JSON body is rejected; a large **file upload** (up to
  1 GB) still works.
- ☐ **8.3 GDB MCP concurrency (B2).** Fire two GDB MCP commands back-to-back through the agent; outputs
  are **not** cross-wired (serialized by the new lock).
- ☐ **8.4 Log-append integrity (B6).** With a subagent active, `findings.md` / `supervisor.log` show no
  torn/interleaved lines (flock).

## 9. Running against the current build

Until the bash entrypoint is retired:

- Use `./scripts/ctfvm <cmd>` instead of `clanker <cmd>`.
- The current build is **Codex-only** — run the Codex cells (A, C) now; the Claude cells (B, D) and the
  `--agent` flag come online in Phase 6.
- Routes are non-versioned (`/api/snapshot`, `/api/send`, …) — see the removed-endpoints table in
  [`API.md`](API.md) for the current → `/api/v1/` mapping.
- Steps 4.5, 5.4, 6.5 (Claude path), 8.1–8.4 are **post-refactor** acceptance and are expected to fail
  on the current build — record them as N/A for a baseline run.

---

## 10. Sign-off

| Cell | Provider | Agent | Date | Build / commit | Result | Failing steps / notes |
|------|----------|-------|------|----------------|--------|-----------------------|
| A | gcp | codex | | | ☐ pass / ☐ fail | |
| B | gcp | claude-code | | | ☐ pass / ☐ fail | |
| C | digitalocean | codex | | | ☐ pass / ☐ fail | |
| D | digitalocean | claude-code | | | ☐ pass / ☐ fail | |

**Release gate:** all four cells pass on the same build, with steps 5.3, 6.1, 6.2, 6.5, 6.6, 7.5
explicitly verified (the bug-regression steps).
