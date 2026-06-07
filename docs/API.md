# Clanker Local Server — API Specification

Base URL: `http://127.0.0.1:<port>` (default port 8765)

All API routes are prefixed `/api/v1/`. The frontend SPA is served at `/`.

This HTTP surface is a **thin layer over the shared core** (`clanker/` — see
[`ARCHITECTURE.md`](ARCHITECTURE.md) §3). The route handlers parse a request, call a core service
(provisioning / snapshot / steering / artifacts / jobs), and serialize the result — no business
logic. Any future surface consumes the same core services directly, so behavior cannot diverge.

Runs carry an **`agent_backend`** (`"codex"` | `"claude-code"`); see [`AGENTS.md`](AGENTS.md). It
appears in run records and is selectable at spawn time.

---

## Conventions

### Request format
- `GET` requests use query parameters.
- `POST` / `DELETE` requests with a body use `Content-Type: application/json`.

### Response envelope
All `/api/v1/` responses return JSON:

```json
{
  "ok": true,
  "data": { ... }
}
```

On error:

```json
{
  "ok": false,
  "error": "human-readable message",
  "code": "ERROR_CODE"
}
```

Error codes:

| Code | Meaning |
|------|---------|
| `NOT_FOUND` | Run ID or artifact not found |
| `BAD_REQUEST` | Missing or invalid parameter |
| `REMOTE_ERROR` | Control plane returned non-zero exit |
| `TIMEOUT` | Remote command timed out |
| `INVALID_PATH` | Artifact path failed security validation |
| `JOB_LIMIT` | Spawn job queue full |
| `AUTH_REQUIRED` | Selected agent backend has no local credentials materialized yet |

### Timestamps
All timestamps are ISO 8601 UTC strings: `"2025-01-15T10:30:00Z"`.

---

## Health

### `GET /health`

No auth. Returns UI server health. Does not contact any VM.

**Response:**
```json
{
  "ok": true,
  "data": {
    "uptime_sec": 3600,
    "known_runs": 3,
    "active_jobs": 1
  }
}
```

---

## Runs

### `GET /api/v1/runs`

List all known runs.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `include_status` | bool | `false` | Include `runtime_status` per run (requires cloud API call per run if not cached) |
| `force_discovery` | bool | `false` | Re-query cloud providers even if discovery cache is fresh |
| `only_live` | bool | `false` | Exclude runs with `runtime_status` of `""` or `"stopped"` |

**Response:**
```json
{
  "ok": true,
  "data": {
    "runs": [
      {
        "run_id": "20250115-103000",
        "provider": "gcp",
        "agent_backend": "claude-code",
        "instance": "ctfvm-myctf-20250115-103000",
        "zone": "us-central1-a",
        "project": "my-project",
        "ip": "34.1.2.3",
        "started_at": "2025-01-15T10:30:00Z",
        "runtime_status": "RUNNING",
        "challenge_state": null
      }
    ],
    "discovered_at": "2025-01-15T11:00:00Z"
  }
}
```

`challenge_state` is `null` unless `include_status=true` and a cached snapshot is available.
`runtime_status` is `""` when `include_status=false`.

---

### `GET /api/v1/runs/{run_id}`

Full snapshot for a single run. Contacts the VM via the control plane.

**Query parameters:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `include_artifacts` | bool | `true` | Include artifact listing |
| `force_status` | bool | `false` | Bypass runtime status cache |

**Response:**
```json
{
  "ok": true,
  "data": {
    "run_id": "20250115-103000",
    "provider": "gcp",
    "instance": "ctfvm-myctf-20250115-103000",
    "zone": "us-central1-a",
    "project": "my-project",
    "ip": "34.1.2.3",
    "started_at": "2025-01-15T10:30:00Z",
    "runtime_status": "RUNNING",

    "findings_tail": "## Progress\n...",
    "supervisor_tail": "[supervisor] starting codex...",

    "panes": [
      {
        "session": "ctf",
        "window_index": 0,
        "window_name": "supervisor",
        "active": true,
        "target": "ctf:supervisor",
        "output": "last 30KB of pane output..."
      }
    ],

    "artifacts": [
      {
        "relpath": "artifacts/exploit.py",
        "size_bytes": 1234,
        "mtime": "2025-01-15T11:05:00Z"
      }
    ],

    "metrics": {
      "snapshot_at": "2025-01-15T11:10:00Z",
      "findings_mtime": "2025-01-15T11:05:00Z",
      "supervisor_mtime": "2025-01-15T11:09:00Z",
      "artifact_mtime": "2025-01-15T11:05:00Z",
      "artifact_count": 3
    },

    "explicit_status": null,

    "challenge_state": {
      "state": "progressing",
      "label": "Active",
      "summary": "Findings updated 2 min ago",
      "last_activity_age_sec": 120,
      "last_activity_label": "findings.md"
    },

    "error": null
  }
}
```

When the VM is unreachable:
```json
{
  "ok": true,
  "data": {
    "run_id": "20250115-103000",
    "...",
    "error": "control plane timeout after 30s",
    "challenge_state": {
      "state": "halted",
      ...
    }
  }
}
```

Note: `ok: true` even when `error` is set — the run record exists but the VM is not responding.
Use `error` field to distinguish partial vs full snapshots.

The snapshot also carries an **`agent_features`** object — the backend-neutral surface of the agent's
normal features (pending approval, session id, MCP servers, skills, model/mode, plan, diffs, usage),
with a `capabilities` list so the SPA only renders controls the backend actually supports. See
[`AGENTS.md`](AGENTS.md) §6b for the shape and the Codex/Claude mapping.

---

### `POST /api/v1/runs`

Spawn one or more new VMs.

**Request body (single):**
```json
{
  "provider": "gcp",
  "agent_backend": "claude-code",
  "zone": "us-central1-a",
  "project": "my-project",
  "challenge_dir": "/path/to/challenge",
  "description": "heap exploit challenge",
  "ideas": "try UAF, try off-by-one",
  "machine_type": "n2-standard-4",
  "toolbox_variant": "lean",
  "no_vpn": false,
  "timeout_min": 1440
}
```

`agent_backend` defaults to `"codex"` if omitted. If the selected backend's credentials are not yet
materialized locally (e.g. no stored Claude OAuth token), the spawn fails with code `AUTH_REQUIRED`
and a `message` containing the exact command to run (`clanker auth claude`). See
[`AGENTS.md`](AGENTS.md) §2.2.

**Request body (batch):**
```json
{
  "batch": [
    { "provider": "gcp", "challenge_dir": "...", "description": "..." },
    { "provider": "digitalocean", "zone": "nyc3", "challenge_dir": "...", "description": "..." }
  ]
}
```

**Response:**
```json
{
  "ok": true,
  "data": {
    "job_ids": ["job-abc123", "job-def456"]
  }
}
```

---

## Run Status

### `POST /api/v1/runs/{run_id}/status`

Set an explicit challenge status override.

**Request body:**
```json
{
  "state": "solved",
  "note": "flag{...} found via heap UAF"
}
```

`state` must be one of `"solved"` or `"blocked"`.

**Response:**
```json
{
  "ok": true,
  "data": {
    "run_id": "20250115-103000",
    "explicit_status": {
      "state": "solved",
      "note": "flag{...} found via heap UAF",
      "updated_at": "2025-01-15T11:15:00Z"
    }
  }
}
```

---

### `DELETE /api/v1/runs/{run_id}/status`

Clear explicit status override. Reverts to automatic challenge state detection.

**Response:**
```json
{
  "ok": true,
  "data": { "run_id": "20250115-103000" }
}
```

---

## Pane Steering

### `POST /api/v1/runs/{run_id}/panes/send`

Send literal text to a tmux pane.

**Request body:**
```json
{
  "target": "ctf:supervisor",
  "text": "try heap UAF path\n",
  "press_enter": true
}
```

`target` format: `<session>:<window>` e.g. `ctf:supervisor`, `ctf:0`, `subagent-abc123:0`.

Validation: target must match `^[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+$`.

**Response:**
```json
{
  "ok": true,
  "data": { "target": "ctf:supervisor" }
}
```

---

### `POST /api/v1/runs/{run_id}/panes/keys`

Send special key sequences to a tmux pane.

**Request body:**
```json
{
  "target": "ctf:supervisor",
  "keys": "C-c"
}
```

`keys` is passed directly to `tmux send-keys`. Allowed values: `C-c`, `C-m`, `C-d`, `Escape`,
`q`, `Enter`.

**Response:**
```json
{
  "ok": true,
  "data": { "target": "ctf:supervisor", "keys": "C-c" }
}
```

---

### `POST /api/v1/runs/{run_id}/panes/trust`

Send the interactive trust confirmation (`1` + Enter) to a pane. Used to accept Codex trust
prompts without human intervention.

**Request body:**
```json
{
  "target": "ctf:supervisor"
}
```

**Response:**
```json
{
  "ok": true,
  "data": { "target": "ctf:supervisor" }
}
```

---

## Artifacts

### `GET /api/v1/runs/{run_id}/artifacts/{path}`

Preview artifact content (up to 2 MB). `{path}` is URL-encoded relative path under `artifacts/`.

**Response:** Raw file bytes with appropriate `Content-Type` header.

On validation failure: `400 Bad Request` with JSON error body.

**Example:**
```
GET /api/v1/runs/20250115-103000/artifacts/artifacts%2Fexploit.py
```

---

### `GET /api/v1/runs/{run_id}/artifacts/{path}/download`

Download the full artifact file.

**Response:** Raw file bytes with `Content-Disposition: attachment` header.

---

### `GET /api/v1/runs/{run_id}/bundle`

Download a `.tar.gz` bundle of the run: `findings.md` + `artifacts/` + `logs/supervisor.log`.

**Response:** `application/gzip` with `Content-Disposition: attachment; filename=<run_id>.tar.gz`.

Capped at `RUN_BUNDLE_MAX_BYTES` (32 MB). Returns `413` if the run exceeds this limit.

---

## Uploads (operator → running run)

### `POST /api/v1/runs/{run_id}/upload`

Push a single file to a running run over the control plane.

**Query params:**
- `path` (required) — destination. A relative path joins the run's workspace
  (`remote_run_dir`, default `/home/ctf/run`); an absolute path must stay inside the
  workspace unless `allow_abs=true`.
- `mode` (optional) — octal file mode, e.g. `0755`.
- `allow_abs` (optional, `true|false`) — permit an absolute destination outside the
  workspace. Off by default; the operator must opt in.

**Body:** raw file bytes (`application/octet-stream`). Capped at `MAX_UPLOAD_BYTES`
(1 GiB) — returns `413` over the limit, `400` for an empty body.

**Response:**
```json
{ "ok": true, "data": { "path": "/home/ctf/run/exploit.py", "size_bytes": 1234, "mode": "0755", "as_tar": false } }
```

Returns `400` (`INVALID_PATH`) when the destination escapes the workspace without
`allow_abs`.

---

### `POST /api/v1/runs/{run_id}/upload-tar`

Upload a tar archive and extract it into a directory on the run.

**Query params:**
- `dest` (required) — destination directory (same workspace-confinement rules as `path` above).
- `allow_abs` (optional) — as above.

**Body:** raw tar bytes. Extraction is traversal-guarded on the VM
(`_safe_extract_tar`). Same size cap as `/upload`.

**Response:** `{ "ok": true, "data": { "path": "<dest>", "size_bytes": N, "mode": "", "as_tar": true } }`

CLI equivalent: `clanker upload <run> <local> [remote] [--tar] [--allow-abs] [--mode 0755]`.

---

## Workers

A **worker** is an empty VM (golden image, control plane up, no agent) that hosts many
challenges. Each hosted challenge is its own run (own workspace + tmux session) sharing
the worker's control endpoint. See docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md.

### `GET /api/v1/workers`

List workers and the challenges hosted on each.

```json
{ "ok": true, "data": { "workers": [
  { "worker": { "run_id": "...", "name": "worker-01", "runner_type": "worker", ... },
    "challenges": [ { "run_id": "...", "name": "pwn-01", "parent_worker_id": "...",
                      "tmux_session": "pwn-01:supervisor", ... } ] } ] } }
```

### `POST /api/v1/workers`

Spawn N empty workers. Body: `{ "count": 5, "provider": "digitalocean", ... }`.
`count` is **required** (1..`MAX_SPAWN_JOBS`). Returns `{ "job_ids": [...] }`.

### `POST /api/v1/workers/{worker_id}/challenges`

Add a challenge to a worker: creates its workspace, uploads the challenge, stages the
agent config, **injects this challenge's credentials at launch**, and starts the agent in
its own tmux session.

- **Query:** `name`, `agent_backend`, `description`, `model`, `reasoning_effort`, `account`.
- **Body:** raw tar of the challenge folder (optional).
- **Response:** `{ "run_id", "slug", "tmux_session", "workspace", "parent_worker_id" }`.

Returns `400` if the target run is not a worker; `AUTH_REQUIRED` if the chosen backend has
no credentials.

### `DELETE /api/v1/workers/{worker_id}/challenges/{slug}`

Stop a challenge's agent session and drop its run record (workspace left on disk).

CLI: `clanker worker {spawn,ls,show,add,rm-challenge}`.

---

## Spawn Jobs

### `GET /api/v1/jobs/{job_id}`

Get the status of a spawn job.

**Response:**
```json
{
  "ok": true,
  "data": {
    "job_id": "job-abc123",
    "state": "running",
    "run_id": null,
    "started_at": "2025-01-15T10:30:00Z",
    "finished_at": null,
    "output_tail": "Creating instance ctfvm-...\nWaiting for control plane..."
  }
}
```

`state` values: `"queued"`, `"running"`, `"done"`, `"error"`.
`run_id` is populated once the run ID is detected in job output.

---

### `GET /api/v1/jobs`

List all known spawn jobs (most recent first, up to `MAX_SPAWN_JOBS`).

**Response:**
```json
{
  "ok": true,
  "data": {
    "jobs": [
      {
        "job_id": "job-abc123",
        "state": "done",
        "run_id": "20250115-103000",
        "started_at": "2025-01-15T10:30:00Z",
        "finished_at": "2025-01-15T10:35:00Z"
      }
    ]
  }
}
```

---

## Subagents

### `GET /api/v1/runs/{run_id}/subagents`

List the run's subagents (uniform across both agent backends — see [`AGENTS.md`](AGENTS.md) §5).

**Response:**
```json
{
  "ok": true,
  "data": {
    "subagents": [
      {
        "id": "f0c3...-uuid",
        "tmux_target": "subagent-f0c3...:0",
        "kind": "exploit_tester",
        "label": "heap UAF hypothesis",
        "state": "running",
        "steerable": true
      }
    ]
  }
}
```

`steerable` is `true` when the subagent has its own resumable tmux session (always true for
platform-spawned sessions; for Claude Code's *native* Task-tool subagents it is `false` and the entry
is read-only). Steer a steerable subagent via `POST /api/v1/runs/{run_id}/panes/send` with its
`tmux_target`.

---

## Removed Endpoints

These are the **actual current routes** (verified in `web.py`). Current routes are non-versioned and
mix HTML pages with `/api/*` JSON. In the refactor all JSON routes move under `/api/v1/`, the dead
route is deleted, and the non-portable Finder picker is cut.

| Current route | Disposition | Replacement |
|---------------|-------------|-------------|
| `GET /` (overview page) | kept | SPA at `GET /` |
| `GET /run` (run page) | kept | SPA route `GET /?run=<id>` |
| `GET /artifacts` (**dead** — wrong renderer, B3) | **removed** | folded into SPA |
| `GET /api/overview` | replaced | `GET /api/v1/runs?include_status=true` |
| `GET /api/runs` | replaced | `GET /api/v1/runs` |
| `GET /api/snapshot?run_id=` | replaced | `GET /api/v1/runs/{run_id}` |
| `GET /api/run-detail?run_id=` | replaced | `GET /api/v1/runs/{run_id}?include_artifacts=false` |
| `GET /api/artifacts-index?run_id=` | replaced | `GET /api/v1/runs/{run_id}` (artifacts inline) |
| `GET /api/artifact?run_id=&path=` | replaced | `GET /api/v1/runs/{run_id}/artifacts/{path}` |
| `GET /api/artifact/download?run_id=&path=` | replaced | `GET /api/v1/runs/{run_id}/artifacts/{path}/download` |
| `GET /api/run-bundle?run_id=` | replaced | `GET /api/v1/runs/{run_id}/bundle` |
| `POST /api/spawn` | replaced | `POST /api/v1/runs` |
| `POST /api/select-directory` (macOS picker) | **removed** | user types path / web file field |
| `POST /api/status-marker` | replaced | `POST /api/v1/runs/{run_id}/status` |
| `POST /api/status-marker/clear` | replaced | `DELETE /api/v1/runs/{run_id}/status` |
| `POST /api/send` | replaced | `POST /api/v1/runs/{run_id}/panes/send` |
| `POST /api/key` | replaced | `POST /api/v1/runs/{run_id}/panes/keys` |
| `POST /api/trust` | replaced | `POST /api/v1/runs/{run_id}/panes/trust` |

---

## Frontend (SPA)

Served at `GET /`. Single HTML file with embedded CSS and JS — no external dependencies.

Routes (client-side):
- `/` → fleet overview (all runs)
- `/?run=<run_id>` → focused view: Findings | Panes | Artifacts | Supervisor Logs

The frontend polls `GET /api/v1/runs` every 30 s for the fleet view and
`GET /api/v1/runs/{run_id}` every 8 s for the focused view.
