# Proposal: Golden-image boot, worker VMs, and operator file uploads

**Status:** Implemented and **smoke-tested live on DigitalOcean (2026-06-07)** — full path
green end-to-end. GCP bake remains untested (fallback covers it).

### Implementation status

| Piece | State |
|---|---|
| Operator uploads (model/service/API/CLI/UI) | ✅ done + unit-tested (`test_server`, `test_commands`) |
| Worker data model on `RunRecord` | ✅ done + tested (`test_state_parity`) |
| Golden-image metadata + `clanker image status/id/bake/record` | ✅ done + tested (`test_goldenimage`) |
| Worker service/API/CLI/UI (spawn/add/remove/list, per-challenge snapshot filter) | ✅ done + tested (`test_workers`) |
| Dockerless `supervisor.sh` + subagent bridge (Docker-fallback aware) | ✅ **smoke-tested on DO** |
| `images/golden/{bake.sh,install-toolbox.sh}` | ✅ **DO bake verified**; GCP untested (fallback) |
| `ctfvm start --worker` provisioning | ✅ **smoke-tested on DO** |

**Live smoke (DO, attended):** baked the golden image, spawned a worker (dockerless boot,
no Docker pull), placed **3 challenges on the one worker** — `01-strings`, `02-base64`,
`pwn-overflow` — each in its own isolated tmux session with per-challenge credential
injection. All three agents (codex) recovered the correct flags; UI grouping + per-challenge
pane isolation confirmed; droplet destroyed at the end. Fixes found during the run: golden
EXIT-trap cleanup, `/workspace`→`RUN_DIR` env + config remap for the dockerless agent, and
worker resolution by name.

Three related changes, decided together because they interact:

1. **Golden image + drop Docker.** Bake a VM image once with all tools + agent CLIs
   pre-installed; run the agent directly on the host. **DigitalOcean first** (the tested
   path); GCP implemented but best-effort/untested. Surface *when the image was last
   built* and *how to rebuild*. The image carries **no credentials and no agent state** —
   creds are injected per-challenge at launch (§1.5).
2. **Worker VMs.** A new `worker` runner type: spawn N **empty** VMs up front, then add
   challenges to them on demand. **Multiple challenges per worker** (accepted as
   not-ideal). No auto-dispatch queue — the operator places challenges manually.
3. **Operator file uploads** — push files to a running VM from the UI/CLI. This is also
   the substrate for "add a challenge to a worker."

Explicit non-goals: a load-balancing dispatcher / work queue (operator places challenges
by hand), and a "fleet" grouping abstraction (dropped).

---

## Part 1 — Golden-image boot, Dockerless

### 1.1 Where start time goes today

From `vm/startup.sh` and the provisioning path:

| Cost | Cold-start | Eliminated by golden image |
|---|---|---|
| VM provision (GCP/DO) | 30–60s | No (provider floor) |
| `apt-get install` (gdb, nodejs, sagemath, radare2, …) | 60–120s | **Yes** (pre-baked) |
| Agent CLI `npm install` | 20–40s | **Yes** (pre-baked) |
| Docker image pull/build | 30–120s | **Yes** (no Docker) |
| `docker run` + per-`exec` | 5–20s | **Yes** (no Docker) |

The container is **not a security boundary** (`docs/ARCHITECTURE.md` §5.3); its only real
jobs are tool-bundling, a venv, and a cheap per-run reset. Dropping it saves ~35–140s;
the **golden image is the big lever** (pre-bakes apt + npm + tools). Target cold start ≈
VM boot + control-plane-ready.

> ⚠️ Removing Docker *without* a golden image would be **slower** (you'd `apt install
> sagemath` every boot). The two ship together.

### 1.2 Build-once model + surfacing

The image is **built once** and reused for every worker/run. Rebuild only when the
toolset or agent CLIs need updating.

- **Bake script** `images/golden/bake.sh --provider digitalocean`:
  boots a throwaway builder droplet → runs the install script (today's
  `images/ctf-toolbox/Dockerfile` package list + `uv` + agent CLIs + gdb MCP + config
  templates + IDA from `CTFVM_IDA_INSTALLER_URL` or `CTFVM_IDA_INSTALLER_PATH` + a
  pre-warmed venv) → captures a **DO custom image** (`doctl compute image`)
  → writes `.ctfvm/golden-image.json` = `{provider, image_id, built_at, agent_versions}`.
  The image is **credential-free**: only binaries, tools, and non-secret config templates
  — never an OAuth token, API key, or `auth.json` (§1.5).
- **GCP** is implemented the same way (`--provider gcp`, `gcloud compute images create`)
  but is **untested by the operator** — the stock-distro fallback (§1.5) covers a missing
  or stale GCP image.
- **Surfacing "last built":**
  - `clanker image status` — reads `.ctfvm/golden-image.json` and confirms against the
    provider (`doctl compute image get`) for the authoritative `created_at`; prints
    `golden image (digitalocean): clanker-toolbox-20260606, built 3 days ago,
    codex 0.42.1 / claude-code 1.2.3`.
  - A one-line footer in the web UI showing the same.
  - On spawn, the run records which image id it booted (repro/debugging).
- **Rebuild:** `clanker image bake --provider digitalocean` (and `--provider gcp`). The
  docs (and `clanker image status` when the image is older than a threshold, e.g. 30
  days) nudge the operator to re-bake.

### 1.3 What moves out of the container

| Today (container) | After (host / golden image) |
|---|---|
| Dockerfile package list | Baked into the image |
| Codex/Claude CLIs in `/opt/ctfvm/npm-global` (ro mount) | Pre-baked in image `$PATH`; no mount |
| gdb MCP at `/opt/ctf-toolbox/mcp/gdb_mcp.py` | Staged to a host path in the image |
| Config templates | Staged by slim `startup.sh` into `~/.codex` / `~/.claude` |
| `/workspace` = bind mount of `/home/ctf/run` | `/home/ctf/run/<slug>` directly (per-challenge; see Part 2/3) |
| `supervisor.sh` wraps `docker exec … tmux` | Runs the agent in tmux as `ctf` directly |
| `subagent-tmux-bridge.sh` does `docker exec … codex resume` | Plain `codex resume` in a tmux session |
| venv at `/workspace/.venv` | Pre-baked base venv; per-challenge venv under its workspace |

### 1.4 Host concerns

- **gdb / ptrace:** bake `kernel.yama.ptrace_scope=0` into the image (same-user ptrace
  works regardless; this enables broader attach). Verify under live smoke.
- **sudo:** `ctf` keeps passwordless sudo (unchanged).
- **Networking:** challenge services bind directly on the host — no bridge NAT. Simpler
  and faster, and it helps the WireGuard-routed-challenge path. (See §3.4 for the
  multi-challenge port caveat.)
### 1.5 Credentials are injected per-challenge, never baked

The golden image **must not** contain credentials — a reusable, snapshot-able image is the
wrong place for a secret. This matches the *existing* launch-time injection model, so no
new mechanism is needed; it just moves from "at VM provision" to "at challenge add":

- `agents/claude_code.py` authenticates via an injected `CLAUDE_CODE_OAUTH_TOKEN` /
  `ANTHROPIC_API_KEY` env var — explicitly *"never by copying credential files."*
- `agents/codex.py` injects `OPENAI_API_KEY`, or stages `auth.json` / `installation_id`
  (unless `no_auth_sync`).
- Source of truth stays `secretstore.py` (`.ctfvm/secrets.json`, `0600`), incl. per-name
  credential **profiles** — so different challenges on the same worker can even use
  different subscriptions.

Flow: spawn an empty worker (image up, **no creds, no agent**) → on **challenge add**,
inject that challenge's creds into its own session `$HOME` (`/home/ctf/run/<slug>`) at
agent launch → on challenge remove, the session's creds die with its workspace.

### 1.6 Fallback + invariants

If `CTFVM_GOLDEN_IMAGE_DO` / `_GCP` is unset or the image is missing, boot the **stock
distro image** and run the old slow install at startup (logged, not fatal). Invariants
unchanged: control plane is the only hot path (1), `RunRegistry` sole state owner (2),
wire shapes in `models.py` (3). `$HOME` and tmux session names stay structured so
snapshot/steering keep working (now per-challenge — Part 3).

---

## Part 2 — Operator file uploads

### 2.1 Already exists (≈90%)

- `vm/control_server.py`: authenticated `POST /files/upload?path=&mode=` and
  `POST /files/upload-tar?dest=` (1 GB cap; `_safe_extract_tar` blocks traversal).
- `clanker/controlclient.py`: `upload_file()` / `upload_tar()`. Used during provisioning.
- No operator-facing path to push files to a *running* VM in API/UI/CLI today.

### 2.2 Net-new (thin wrapper)

- **API** (`server/app.py`): `POST /api/v1/runs/{id}/upload` (raw body + `path`) and
  `POST /api/v1/runs/{id}/upload-tar?dest=`.
- **Service** (`server/service.py`): `upload(run_id, dest, body, *, mode, as_tar)`.
- **CLI** (`commands.py`): `clanker upload <run> <local> [remote] [--tar]`.
- **UI** (`frontend/index.html`): a drop zone / file picker in the run panel.

### 2.3 Guardrails

- **Default-confine to the run's workspace**; writing elsewhere needs `--allow-abs`
  (the control endpoint can write any path/mode).
- Reject early on `Content-Length`; surface the cap.
- **Audit** operator uploads server-side (run_id, dest, size). Keep tar-traversal guard.
- New `UploadResult { path, size, mode }` in `models.py` (Inv. 3).

This same plumbing is what "add a challenge to a worker" (§3.3) is built on.

---

## Part 3 — Worker VMs (the new runner type)

> A **worker** is a long-lived VM that boots empty (control plane up, **no agent
> running**). The operator spawns a handful up front, then drops challenges onto them as
> needed. Each challenge runs as its **own agent session in its own workspace** on the
> worker. Workers are *not* a queue and *not* a fleet — just reusable hosts.

### 3.1 Why this shape

We removed Docker because **one VM per challenge** made the container's reset redundant.
Workers reintroduce *multiple challenges per VM*, which is exactly where a per-challenge
reset would have helped — but you've accepted the trade. So isolation is by **workspace
directory + separate agent session**, not by container. §3.4 states precisely what is and
isn't isolated.

### 3.2 Data model (small additions to `RunRecord`)

Reuse the existing per-run snapshot/steering pipeline by making a challenge-on-a-worker
its **own run record** that shares the worker's control endpoint:

```python
# RunRecord (models.py) — additions, all default-safe for existing single runs
runner_type: str = "challenge"     # "challenge" (single, today's default) | "worker"
parent_worker_id: str = ""         # set on challenge records hosted by a worker
tmux_session: str = "ctf:supervisor"  # promoted from a hardcoded constant
```

- **Worker record:** `runner_type="worker"`, no challenge bound, `remote_run_dir=/home/ctf/run`,
  named `worker-01..N`. Its snapshot lists hosted challenges + host stats (not a single
  agent pane).
- **Challenge-on-worker record:** `runner_type="challenge"`, `parent_worker_id=<worker>`,
  **control_* copied from the worker** (same VM), own `run_id`, `challenge_name`,
  `agent_backend`, `remote_run_dir=/home/ctf/run/<slug>`, `tmux_session=<slug>:supervisor`.

Because `remote_run_dir` and (now) `tmux_session` come from the record, existing
`snapshot.py` / `steering.py` / `artifacts.py` work per challenge with minimal change —
the only edit is sourcing the session name from the record instead of the `ctf:supervisor`
constant. Honors Inv. 2 (still just run-state files, no new store) and Inv. 3.

### 3.3 Lifecycle

```
spawn empty workers ──► add challenge ──► monitor per challenge ──► remove / destroy
   (golden image,        (upload + launch    (own snapshot,          (stop session, or
    no agent)             agent session)      transcript, panes)      tear down VM)
```

1. **Spawn N empty workers** — provision N VMs from the golden image, start the control
   plane, **skip the agent launch**. Register N `worker` records.
2. **Add a challenge** — upload the challenge to `/home/ctf/run/<slug>` (Part 2 plumbing),
   render the agent config into that workspace, **inject that challenge's credentials**
   into the session `$HOME` at launch (§1.5 — env token / staged `auth.json`, optionally a
   chosen secretstore profile), `tmux new-session -d -s <slug>:supervisor` running the
   agent there, register a `challenge` record with `parent_worker_id`. `supervisor.sh`
   becomes parameterized by `(slug, workspace, session, backend)` and invokable many times
   per VM; `subagent-tmux-bridge.sh` namespaces child sessions as `subagent-<slug>-<uuid>`.
3. **Monitor** — each challenge is a focusable run with its own snapshot/transcript/panes,
   unchanged. The worker view rolls up its challenges.
4. **Remove a challenge** — kill its tmux session; optionally `rm -rf` its workspace.
5. **Destroy a worker** — tear down the VM and all hosted challenge records.

### 3.4 Isolation boundary (read before relying on it)

With multiple challenges on one Dockerless worker:

| Isolated per challenge | **Shared / leaks across challenges on the same worker** |
|---|---|
| Workspace dir `/home/ctf/run/<slug>` | Kernel, `sudo` |
| Per-challenge venv, `$HOME`, agent state, creds | **Network ports** (two challenges binding `:1337` collide) |
| tmux session + subagent sessions | **Globally `apt install`ed packages** (additive, usually benign) |
| Agent transcript / findings / artifacts | System services, CPU / RAM / disk contention |

**Decision:** no extra isolation layer. **Port collisions are left for the agents to
resolve** at runtime (pick a free port, etc.) — accepted per operator. We do *not* add
per-user sandboxing or namespaces; that would reintroduce container-like complexity and
contradict the golden-image decision. Workspace + session + credential separation is the
boundary; everything in the right column is shared by design.

### 3.5 API (additive)

```
POST   /api/v1/workers                       {count, provider, ...}  -> spawn N empty workers
GET    /api/v1/workers                        -> workers + hosted-challenge counts
GET    /api/v1/workers/{id}                   -> worker + hosted challenge records
POST   /api/v1/workers/{id}/challenges        {dir|upload, agent_backend, prompt} -> add
DELETE /api/v1/workers/{id}/challenges/{slug} -> stop + optional cleanup
POST   /api/v1/workers/{id}/destroy           -> tear down VM + all challenges
GET    /api/v1/runs/{challenge_run_id}/...     -> existing snapshot/steering, unchanged
```

### 3.6 CLI

```
clanker worker spawn --count 5 --provider digitalocean   # N empty workers
clanker worker ls                                         # workers + counts
clanker worker show <worker>                              # hosted challenges
clanker worker add <worker> --dir ./challenges/pwn-01     # upload + launch
clanker worker rm-challenge <worker> <slug>               # stop a challenge
clanker worker destroy <worker>                           # tear down the VM
```

### 3.7 UI

Workers appear in the sidebar; expanding one lists its hosted challenges (each a focusable
run). An "＋ add challenge" control sits on the worker header; a host-stats / challenge-count
roll-up shows on the worker row. Selecting a challenge focuses its snapshot exactly as a
single run does today.

```
┌──────────────────────────────────────────────┐
│  ▼ worker-03   do · 2 challenges   ＋ add  ⛔  │
│  ├ pwn-01    ◐ running   codex                 │
│  └ web-04    ⚠ stalled   claude                │
│  ▼ worker-01   do · 0 challenges   ＋ add  ⛔  │
│     (empty — ready for work)                   │
│  ▶ worker-02   do · 1 challenge                │
└──────────────────────────────────────────────┘
```

---

## Sequencing & effort

| # | Change | Effort | Risk | Notes |
|---|---|---|---|---|
| 2 | Operator uploads | **S** | Low | plumbing exists; substrate for worker add |
| 1 | Golden image (DO) + Dockerless | **L** | Medium | needs **attended** live smoke test |
| 3 | Worker runner type | **M–L** | Medium | depends on Dockerless multi-session + uploads |
| 1b| Golden image (GCP) | **S–M** | Low | mirror of DO; untested → fallback covers it |

Recommended order: **uploads → golden image (DO) → workers → GCP image**. Uploads is pure
local/UI work, independently shippable. The golden image and Dockerless supervisor are
prerequisites for empty workers, and need an **attended** live smoke test on DigitalOcean
(per `live-smoke-test-must-be-attended`) before the Docker paths are deleted.

## Decisions locked

- **Golden image carries no credentials**; creds injected per-challenge at launch (§1.5).
- **Worker spawn always requires explicit `--count`** — no default.
- **Port collisions are the agents' problem** — no per-user/namespace isolation (§3.4).
- **DigitalOcean first**, GCP implemented but untested (fallback covers it).

## Open questions

- `clanker image status` staleness nudge — warn at 30 days, or just always show the date?
- Workspace confinement for uploads — `--allow-abs` escape hatch as the default? (confirm)

## Tests to add (regression test per change — stdlib `unittest`)

- Uploads: path-confinement (reject abs without opt-in), size guard, tar-traversal blocked.
- Workers: `RunRecord` round-trips `runner_type` / `parent_worker_id` / `tmux_session`;
  a challenge record inherits the worker's control endpoint; `worker destroy` targets
  exactly the worker's challenge run-ids; two challenges get distinct tmux sessions.
- Boot: bake-script package parity vs. the old Dockerfile list (diff empty); golden-image
  metadata round-trips; missing-image falls back to stock distro.
