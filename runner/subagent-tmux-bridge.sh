#!/usr/bin/env bash
set -euo pipefail

RUN_DIR="/home/ctf/run"
POLL_SEC="2"
SESSION_PREFIX="subagent"

usage() {
  cat <<USAGE
Usage:
  subagent-tmux-bridge.sh [--run-dir /home/ctf/run] [--poll-sec 2] [--session-prefix subagent]

Behavior:
  - Watches Codex session logs for spawned child agent ids ("agent_id").
  - Creates one tmux session per discovered child id:
      <session-prefix>-<agent_id>
  - Each spawned session runs:
      docker exec -it ctf-toolbox bash -c 'cd /workspace && codex resume <agent_id> --no-alt-screen'
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      RUN_DIR="$2"; shift 2 ;;
    --poll-sec)
      POLL_SEC="$2"; shift 2 ;;
    --session-prefix)
      SESSION_PREFIX="$2"; shift 2 ;;
    -h|--help)
      usage
      exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage >&2
      exit 1 ;;
  esac
done

if [[ ! "${POLL_SEC}" =~ ^[0-9]+$ || "${POLL_SEC}" -lt 1 ]]; then
  echo "Invalid --poll-sec value: ${POLL_SEC}" >&2
  exit 1
fi
if [[ ! "${SESSION_PREFIX}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid --session-prefix value: ${SESSION_PREFIX}" >&2
  exit 1
fi
if [[ ! -d "${RUN_DIR}" ]]; then
  echo "Run directory not found: ${RUN_DIR}" >&2
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found." >&2
  exit 1
fi
# Use the container if present, else resume the subagent directly on the host
# (golden-image / worker path).
USE_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ctf-toolbox$'; then
  USE_DOCKER=1
fi

STATE_DIR="${RUN_DIR}/.subagent-bridge"
SESSIONS_DIR="${RUN_DIR}/.codex/sessions"
MARKER_FILE="${STATE_DIR}/start.marker"
SEEN_FILE="${STATE_DIR}/seen-agent-ids.txt"
COMPLETED_FILE="${STATE_DIR}/completed-agent-ids.txt"
MAP_FILE="${STATE_DIR}/session-map.tsv"
LOG_DIR="${RUN_DIR}/logs"

mkdir -p "${STATE_DIR}" "${LOG_DIR}"
: > "${MARKER_FILE}"
touch "${SEEN_FILE}" "${COMPLETED_FILE}" "${MAP_FILE}"

timestamp() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

already_seen() {
  local agent_id="$1"
  grep -qx "${agent_id}" "${SEEN_FILE}" 2>/dev/null
}

mark_seen() {
  local agent_id="$1"
  if ! already_seen "${agent_id}"; then
    printf '%s\n' "${agent_id}" >> "${SEEN_FILE}"
  fi
}

already_completed() {
  local agent_id="$1"
  grep -qx "${agent_id}" "${COMPLETED_FILE}" 2>/dev/null
}

mark_completed() {
  local agent_id="$1"
  if ! already_completed "${agent_id}"; then
    printf '%s\n' "${agent_id}" >> "${COMPLETED_FILE}"
  fi
}

session_name_for() {
  local agent_id="$1"
  printf '%s-%s\n' "${SESSION_PREFIX}" "${agent_id}"
}

rollout_file_for_agent() {
  local agent_id="$1"
  find "${SESSIONS_DIR}" -type f -name "rollout-*-${agent_id}.jsonl" 2>/dev/null | head -n1
}

agent_task_completed() {
  local agent_id="$1"
  local rollout_file

  rollout_file="$(rollout_file_for_agent "${agent_id}")"
  if [[ -z "${rollout_file}" || ! -f "${rollout_file}" ]]; then
    return 1
  fi

  grep -q '"type":"task_complete"' "${rollout_file}" 2>/dev/null \
    || grep -q '"phase":"final_answer"' "${rollout_file}" 2>/dev/null
}

collect_agent_ids() {
  if [[ ! -d "${SESSIONS_DIR}" ]]; then
    return 0
  fi
  find "${SESSIONS_DIR}" -type f -name 'rollout-*.jsonl' -newer "${MARKER_FILE}" -print0 2>/dev/null \
    | while IFS= read -r -d '' f; do
        grep -h -oE '"agent_id":"[0-9a-fA-F-]{36}"' "${f}" 2>/dev/null | sed -E 's/^"agent_id":"([^"]+)"$/\1/' || true
        grep -h -oE '\\\\"agent_id\\\\":\\\\"[0-9a-fA-F-]{36}\\\\"' "${f}" 2>/dev/null | sed -E 's/^\\\\"agent_id\\\\":\\\\"([^"]+)\\\\"$/\1/' || true
        if grep -q '"thread_spawn"' "${f}" 2>/dev/null; then
          basename "${f}" | sed -E 's/^.*-([0-9a-fA-F-]{36})\.jsonl$/\1/' || true
        fi
      done \
    | tr 'A-F' 'a-f' \
    | grep -E '^[0-9a-f-]{36}$' \
    | sort -u
}

spawn_session_for_agent() {
  local agent_id="$1"
  local session_name launcher log_file

  if [[ ! "${agent_id}" =~ ^[0-9a-f-]{36}$ ]]; then
    return 0
  fi

  session_name="$(session_name_for "${agent_id}")"
  if tmux has-session -t "${session_name}" 2>/dev/null; then
    mark_seen "${agent_id}"
    return 0
  fi

  launcher="${STATE_DIR}/launch-${agent_id}.sh"
  log_file="${LOG_DIR}/${session_name}.log"

cat > "${launcher}" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
echo "[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] session ${session_name} starting for agent ${agent_id}" | tee -a "${log_file}"
if [[ "${USE_DOCKER}" == "1" ]]; then
  docker exec -it ctf-toolbox bash -c 'cd /workspace && codex resume ${agent_id} --no-alt-screen'
else
  ( cd "${RUN_DIR}" && HOME="${RUN_DIR}" PATH="${CTFVM_NPM_PREFIX:-/opt/ctfvm/npm-global}/bin:\$PATH" codex resume ${agent_id} --no-alt-screen )
fi
rc=\$?
echo "[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] session ${session_name} exited rc=\${rc}" | tee -a "${log_file}"
exit "\${rc}"
SCRIPT
  chmod +x "${launcher}"

  tmux new-session -d -s "${session_name}" "bash -lc '${launcher}'"
  printf '%s\t%s\n' "${agent_id}" "${session_name}" >> "${MAP_FILE}"
  mark_seen "${agent_id}"
  echo "[$(timestamp)] spawned tmux session ${session_name} for ${agent_id}"
}

reap_completed_sessions() {
  local agent_id session_name

  while IFS=$'\t' read -r agent_id session_name; do
    [[ -z "${agent_id}" || -z "${session_name}" ]] && continue
    if already_completed "${agent_id}"; then
      continue
    fi
    if ! agent_task_completed "${agent_id}"; then
      continue
    fi
    mark_completed "${agent_id}"
    if tmux has-session -t "${session_name}" 2>/dev/null; then
      tmux kill-session -t "${session_name}" >/dev/null 2>&1 || true
      echo "[$(timestamp)] auto-closed tmux session ${session_name} after task completion for ${agent_id}"
    else
      echo "[$(timestamp)] marked ${agent_id} completed; tmux session ${session_name} was already gone"
    fi
  done < "${MAP_FILE}"
}

echo "[$(timestamp)] subagent tmux bridge started (run_dir=${RUN_DIR}, poll_sec=${POLL_SEC}, session_prefix=${SESSION_PREFIX})"

while true; do
  if ! docker ps --format '{{.Names}}' | grep -q '^ctf-toolbox$'; then
    sleep "${POLL_SEC}"
    continue
  fi

  while IFS= read -r agent_id; do
    [[ -z "${agent_id}" ]] && continue
    if already_seen "${agent_id}"; then
      continue
    fi
    spawn_session_for_agent "${agent_id}"
  done < <(collect_agent_ids || true)

  reap_completed_sessions

  sleep "${POLL_SEC}"
done
