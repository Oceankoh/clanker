# Full Test — UI-API Matrix

End-to-end acceptance: starting from **zero runs**, drive the platform **only through the
`/api/v1` UI-button endpoints** (no `smoke.sh`, no SSH) and confirm every agent × runtype solves
every challenge, the system prompt is delivered, steering reaches the agent, and teardown is clean.

`smoke.sh` covers single-VM normal runs only; this suite is the authoritative gate for the **worker**
path and the cross-product matrix.

## Last full test

| field | value |
|-------|-------|
| **Date** | _pending re-run_ |
| **Commit** | _pending_ (prev partial: `70cdead`+working-tree, 2026-06-08) |
| **Result** | _pending_ (prev: 16/20 attempted — claude·normal blocked by folder-trust bug, since fixed) |
| **Run by** | operator + Claude Code, attended |

## Tickets (matrix)

Each cell is a ticket: ✅ pass / ❌ fail / ⬜ not-yet-run, with the date last green.
A cell passes when its flag (from `EXPECTED.tsv`) lands **and** a steering message reached the agent.

| challenge (flag) | codex·normal | claude·normal | codex·worker | claude·worker |
|------------------|:---:|:---:|:---:|:---:|
| 01-strings  | ✅ 06-08 | ⬜ | ✅ 06-08 | ✅ 06-08 |
| 02-base64   | ✅ 06-08 | ⬜ | ✅ 06-08 | ✅ 06-08 |
| 03-caesar   | ⬜ | ⬜ | ✅ 06-08 | ✅ 06-08 |
| 04-hidden   | ✅ 06-08 | ⬜ | ✅ 06-08 | ✅ 06-08 |
| pwn-overflow (gdb-MCP) | ⬜ | ⬜ | ✅ 06-08 | ✅ 06-08 |
| web-local (VPN) | ✅ 06-08 | ⬜ | ✅ 06-08 | ✅ 06-08 |

Cross-cutting tickets (one pass covers the matrix):
- ⬜ **system-prompt delivered** — agent transcript shows the challenge description.
- ✅ 06-08 **steering reaches agent** — a sent token appears in the target's transcript.
- ⬜ **UI renders + drives** — open the web UI: fleet lists runs, workers group their challenges
  (non-clickable host showing Ready/Provisioning pill), status pills update, and a steering message
  sent from the steer box reaches the agent. Use the share link or `http://127.0.0.1:8765/?token=…`.
- ✅ 06-08 **ngrok share + token** — valid token→200 (serves UI), no/wrong token→401.
- ✅ 06-08 **teardown** — `destroy --all` removes every droplet (verified via `doctl`), local state cleared.

## Prerequisites (once)

- `doctl auth init` (DigitalOcean — GCP needs `gcloud`, not required here).
- Agent auth: `codex` logged in (`~/.codex/auth.json`) and/or Claude OAuth in `.ctfvm/secrets.json`.
- `ngrok` on PATH (for the share/token ticket).
- ⚠️ **DigitalOcean caps the account at 10 concurrent droplets.** The full 14-VM matrix doesn't fit
  at once — workers host many challenges on **one** droplet, so lean on them for breadth and recycle
  normal runs (destroy-solved → respawn) for the rest. (See memory `do-droplet-cap`.)

## How to test

All commands assume `clanker serve` is the API target. `reasoning_effort=medium` keeps usage down.

```bash
# 0. Clean slate + start the API the buttons talk to.
ls .ctfvm/runs/ 2>/dev/null            # expect empty; if not, destroy first (step 6)
python -m clanker serve --host 127.0.0.1 --port 8765 &     # background

# 1. WORKERS first (2 droplets, host all 6 challenges each). VPN on by default.
curl -sX POST :8765/api/v1/workers -d '{"count":2,"provider":"digitalocean"}'

# 2. NORMAL runs. Local-only challenges get --no-vpn; web-local keeps VPN.
#    Spawn ≤8 here to stay under the 10-droplet cap (2 workers + 8 normal).
B=$PWD/examples
for ag in codex claude-code; do
  for c in smoke-challenges/01-strings smoke-challenges/02-base64 smoke-challenges/03-caesar \
           smoke-challenges/04-hidden lab-challenges/pwn-overflow; do
    curl -sX POST :8765/api/v1/runs -H 'Content-Type: application/json' \
      -d "{\"challenge_dir\":\"$B/$c\",\"agent_backend\":\"$ag\",\"reasoning_effort\":\"medium\",\"no_vpn\":true}"
  done
  curl -sX POST :8765/api/v1/runs -H 'Content-Type: application/json' \
    -d "{\"challenge_dir\":\"$B/lab-challenges/web-local\",\"agent_backend\":\"$ag\",\"reasoning_effort\":\"medium\"}"
done
# (cap: spawn in batches, recycle solved droplets to cover all 12 normal cells.)

# 3. Add all 6 challenges to each worker (script tars the dir + builds the prompt).
#    <WORKER_ID> from: curl -s :8765/api/v1/workers
scripts/test/add_challenge.py <WORKER1_ID> codex       medium $B/smoke-challenges/0{1,2,3,4}-* $B/lab-challenges/{pwn-overflow,web-local}
scripts/test/add_challenge.py <WORKER2_ID> claude-code medium $B/smoke-challenges/0{1,2,3,4}-* $B/lab-challenges/{pwn-overflow,web-local}
#    NOTE: a 2vCPU/4GB worker OOMs running ~6 agents at once — add in waves of ~3.

# 4. Bring up VPN for the web-local / worker instances (needs your sudo, one session).
#    Start the local target service FIRST so the agent can reach it over the tunnel:
python3 examples/lab-challenges/web-local-serve.py &     # binds 0.0.0.0:8000, serves /flag
sudo ./scripts/ctfvm vpn up                              # only vpn_requested runs (honors --no-vpn)

# 4b. UI check: open the web UI and eyeball it (share link, or serve a tokened URL).
python -m clanker share          # prints https://<ngrok>/?token=… ; open it
#    Confirm: fleet lists runs; the two workers group their challenges; the worker
#    HOST row is non-clickable and shows a Ready/Provisioning pill; status pills
#    update; sending from the steer box reaches the agent (token shows in transcript).

# 5. Drive + assert. Steering test, then poll flags until the matrix is green.
curl -sX POST :8765/api/v1/runs/<RUN_ID>/panes/send -H 'Content-Type: application/json' \
  -d '{"target":"<session>:supervisor","text":"PING-x: reply PING-x","press_enter":true}'   # then check transcript
watch -n20 scripts/test/assert_flags.py        # tally line = found/total

# 6. Teardown — the cost-critical step. Then VERIFY with doctl.
./scripts/ctfvm destroy --all
doctl compute droplet list --no-header | grep -i ctfvm || echo "all destroyed"
# golden-image snapshot (clanker-toolbox-*) is intentionally LEFT (low cost).
# Local VPN interfaces need sudo to remove: sudo pkill -f wireguard-go; sudo pkill -f vpn_dns_forwarder.py
```

`assert_flags.py` matches each run's `EXPECTED.tsv` flag against findings + panes + transcript
(codex reports the flag in its transcript, not `findings.md`).

## Notes / gotchas this suite guards against

- **claude-code folder-trust** (fixed): `.claude.json` trust key must be remapped `/workspace`→`RUN_DIR`
  on normal dockerless runs, else claude hangs on "Do you trust the files in this folder?" (0 transcript
  events). Worker runs remap it before upload. Re-verify: a claude·normal run must produce events + solve.
- **`vpn up` honors `--no-vpn`** (fixed): no-selector `vpn up` skips `vpn_requested=0` runs.
- **worker challenges** share the worker's VM, tunnel, and a synthetic instance name — status/bundle/vpn
  all resolve to the worker; `destroy` sweeps their records via `parent_worker_id`.
- **empty job output**: spawn-job stdout isn't captured — diagnose failed spawns via `doctl` + the run snapshot.
