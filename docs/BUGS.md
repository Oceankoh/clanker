# Verified Bug & Edge-Case Catalogue

Every item below was checked against the live code with file:line evidence. This list **corrects
earlier planning notes** that overstated two security issues — both are already mitigated. Severities:
**P0** breaks correctness/security materially · **P1** real but bounded · **P2** robustness/quality.

The "Fixed by" column points at the phase in [`REFACTOR_PLAN.md`](REFACTOR_PLAN.md).

---

## Refuted claims (NOT bugs — keep the existing protection)

These were labeled "critical" in earlier drafts. The current code already handles them; the refactor
must **preserve** these checks, not "introduce" them.

### R1 — Artifact path traversal — REFUTED
`scripts/ctfvm_ui/service.py:1465` `sanitize_artifact_relpath()` splits the path, **rejects any
component equal to `..`**, strips `.`, rejects absolute/`~` paths, and requires an `artifacts/` prefix:
```python
parts = [p for p in relpath.split("/") if p and p != "."]
if any(p == ".." for p in parts): return None
clean = "/".join(parts)
if not clean.startswith("artifacts/"): return None
```
`artifacts/../../etc/passwd` → contains `..` → rejected. `artifacts/./a/../../..` → `..` present →
rejected. **Not exploitable.**

### R2 — Tmux target shell injection — REFUTED
`service.py:1480` `safe_target()` enforces `^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$`, and the value is
additionally `shlex.quote()`d before reaching `tmux send-keys` (`service.py:~1121`). A target like
`ctf:supervisor;rm -rf /` fails the regex → rejected. **Not exploitable.**

### R3 — Tar extraction escape — REFUTED
`vm/control_server.py:31` `_safe_extract_tar()` resolves every member against the destination and
rejects out-of-dir paths, empty/absolute/escaping link targets, **before** calling `extractall`. The
zombie-process concern is also handled: `_run_shell` uses `proc.communicate(timeout=...)` then
`proc.kill()`+`communicate()` on timeout. **Not exploitable / already correct.**

---

## P0 — correctness/robustness, fix first

### B1 — Snapshot marker collision
`service.py:719-833`. The remote snapshot script emits plaintext markers (`__FINDINGS__`,
`__SUPERVISOR__`, `__PANE_BEGIN__{session}|{index}`, …) and the parser does `str.find()` between
them. If pane output, `findings.md`, or `supervisor.log` contains a marker string (or a pane name
contains `|`), parsing splits at the wrong place and the snapshot is silently corrupted.
**Fix (Phase 4):** single JSON envelope; base64-encode every variable-length field (pane output,
findings, supervisor tail). No marker splitting. → ARCHITECTURE §6 `snapshot.py`, Invariant 4.

### B2 — GDB MCP session not serialized — FIXED (Phase 6, defense-in-depth)
`images/ctf-toolbox/mcp/gdb_mcp.py` shares one global `session` over a single stdout. The current
stdio loop processes messages sequentially (`await handle_message` before the next read), so the race
isn't reachable *today* — but it would be the moment anyone dispatches messages concurrently.
**Fixed:** a module `asyncio.Lock` (`_TOOL_LOCK`) serializes the stateful tools (`gdb_start`/`gdb_exec`/
`gdb_stop`); `interrupt` deliberately bypasses it so it can preempt a blocked `execute`.

### B3 — Dead `/artifacts` route + unreachable page
`web.py:2080` maps `GET /artifacts` to `render_run_page()` (the wrong function); the dedicated
`render_artifacts_page()` (`web.py:1804-2030`, ~225 lines) is never reached.
**Fix (Phase 5):** removed in the API rewrite — artifacts are part of the run snapshot/SPA. Delete the
dead handler and unreachable page. → [`API.md`](API.md) removed-endpoints table.

---

## P1 — real, bounded

### B4 — Provider discovery cache TOCTOU race
`providers.py:384` `ProviderRegistry._discovery_cache` is an unguarded dict; concurrent requests can
both see it stale and both issue cloud-API discovery calls (wasted quota, possible inconsistent merge).
The status cache *is* locked (`service.py:202`), so this is the lone gap.
**Fix (Phase 1):** move discovery + status caching into `RunRegistry` under one `RLock`.
→ ARCHITECTURE §6 `state.py`, Invariant 2.

### B5 — No request logging on the control server — FIXED (Phase 6)
`vm/control_server.py` `log_message` was a no-op `return`. **Fixed:** a structured one-line stderr log
per request (`<ts> <client> <method> <path> <status> <ms>ms`), captured by the systemd journal, plus a
separate 10 MB cap for `/exec` bodies (`MAX_EXEC_BYTES`) vs the 1 GB file-upload cap. Tested in
`tests/test_control_server.py`.

### B6 — `findings.md` / `supervisor.log` appends — DOWNGRADED (re-investigated)
Re-investigation in Phase 6 found the original premise overstated for *our* scripts: `supervisor.sh`
writes the `findings.md` header once **before** launching the agent, and its `tee -a supervisor.log`
calls all run on the single supervisor thread; the subagent bridge writes a **separate** file
(`logs/subagent-bridge.log`). So there is no multi-writer race in the shell we control. The real
concurrent writer to `findings.md` is the **agent itself** (the model + its subagents) inside the
container — which we don't serialize from bash. That is mitigated by convention, not `flock`: the
subagent role instructions direct evidence to per-subagent dirs
(`/workspace/artifacts/subagents/<role>/`). No `flock` added (it would protect a write that isn't
contended); tracked as a prompt/convention concern rather than a bash bug.

### B7 — SSH used whenever control creds are absent (monitoring path)
`providers.py:265-280` (GCP) / `:348-360` (DO): `ssh_cmd()` uses the control plane *if configured*,
else SSH. For snapshot/steering/artifacts this means a run missing control creds silently shifts the
hot path onto SSH — exactly what the two-plane design forbids (Invariant 1), and the source of slow,
inconsistent polling for legacy runs.
**Fix (Phase 4):** monitoring/steering/artifact calls go through `controlclient` only and raise
`ControlPlaneError` when creds are missing (surfaced as the snapshot `error` field). SSH stays only in
`CloudProvider.ssh_break_glass` for `attach`/`shell`/`vscode`.

---

## P2 — quality / maintainability (addressed by the structural refactor)

### B8 — `control_client.py` swallows decode errors, single-attempt, partial timeouts
`scripts/ctfvm_control_client.py`: bare `except Exception` around JSON parsing can mask a real
`/exec` failure as `returncode=1`; no retry on transient network errors; timeout covers read but not
connect/DNS. **Fix (Phase 1):** typed `controlclient.py` with connect+read timeouts and typed errors.

### B9 — 22-positional-arg `write_state` in bash
`scripts/ctfvm:621-675` writes state via a 22-positional-argument function with no validation —
trivially mis-ordered. **Fix (Phase 1/2):** state written through `RunRegistry` in Python.

### B10 — Provider SSH/registry copy-paste
`scripts/lib/ctfvm/providers/{gcp,digitalocean}.sh` duplicate `ssh_exec`/`ssh_tty`/`ssh_argv`/registry
helpers. **Fix (Phase 2):** one `CloudProvider` ABC; shared logic in `providers/base.py`.

### B11 — `startup.sh` codex-cli fallback failure not caught
`vm/startup.sh:69-79`: if `@openai/codex` install fails and the `codex-cli` fallback *also* fails, the
second failure isn't checked before the final `-x` test (which does catch a missing binary, but the
intermediate error is silent). **Fix (Phase 6):** explicit error on fallback failure; this script also
gains a Claude-CLI install step (see AGENTS.md / ARCHITECTURE §5.3).

### B12 — Embedded frontend (~1,800 lines) inside `web.py`
Three `render_*_page()` functions build HTML+CSS+JS as Python strings (`web.py:11-2030`). Untestable,
unlintable, no syntax highlighting, and the dead-route bug (B3) hid here.
**Fix (Phase 5):** extract to `clanker/server/frontend/index.html`, a self-contained SPA.

---

## Bug → phase summary

| ID  | Severity | Area                | Phase | Status |
|-----|----------|---------------------|-------|--------|
| B1  | P0       | snapshot parsing    | 4     | ✅ fixed |
| B2  | P0       | gdb MCP concurrency | 6     | ✅ fixed (defense-in-depth) |
| B3  | P0       | dead route / page   | 5     | ✅ fixed (route gone) |
| B4  | P1       | discovery race      | 1     | ✅ fixed (one RLock) |
| B5  | P1       | control-server logs | 6     | ✅ fixed (+/exec cap) |
| B6  | P1       | log write races     | 6     | ▽ downgraded (not contended in our scripts) |
| B7  | P1       | SSH on hot path     | 4     | ✅ fixed (control-plane only) |
| B8  | P2       | control client      | 1     | ✅ fixed (typed client) |
| B9  | P2       | bash write_state    | 1–2   | ◑ Python core writes state; bash `start` cutover live-deferred |
| B10 | P2       | provider duplication| 2     | ✅ fixed (CloudProvider ABC) |
| B11 | P2       | startup install     | 6     | ✅ fixed (fallback error + Claude install) |
| B12 | P2       | embedded frontend   | 5     | ✅ fixed (extracted index.html) |
| R1–R3 | —      | refuted (preserve)  | n/a   | preserved (asserts in tests) |
