#!/usr/bin/env bash
set -euo pipefail

RUN_DIR="/home/ctf/run"
PROMPT_FILE="/home/ctf/run/challenge_prompt.txt"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      RUN_DIR="$2"; shift 2 ;;
    --prompt-file)
      PROMPT_FILE="$2"; shift 2 ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1 ;;
  esac
done

mkdir -p "${RUN_DIR}/logs" "${RUN_DIR}/artifacts"
touch "${RUN_DIR}/findings.md" "${RUN_DIR}/inject.queue" "${RUN_DIR}/logs/supervisor.log"
chmod 644 "${PROMPT_FILE}" 2>/dev/null || true

BRIDGE_SCRIPT="${RUN_DIR}/subagent-tmux-bridge.sh"
BRIDGE_PID_FILE="${RUN_DIR}/subagent-tmux-bridge.pid"
BRIDGE_LOG="${RUN_DIR}/logs/subagent-bridge.log"
SUBAGENT_TMUX_BRIDGE="${SUBAGENT_TMUX_BRIDGE:-1}"
SUBAGENT_BRIDGE_POLL_SEC="${SUBAGENT_BRIDGE_POLL_SEC:-2}"

start_subagent_bridge() {
  if [[ "${SUBAGENT_TMUX_BRIDGE}" != "1" ]]; then
    echo "Subagent tmux bridge: disabled (SUBAGENT_TMUX_BRIDGE=${SUBAGENT_TMUX_BRIDGE})." | tee -a "${RUN_DIR}/logs/supervisor.log"
    return 0
  fi
  if [[ ! -x "${BRIDGE_SCRIPT}" ]]; then
    echo "Subagent tmux bridge script missing or not executable: ${BRIDGE_SCRIPT}" | tee -a "${RUN_DIR}/logs/supervisor.log"
    return 0
  fi

  if [[ -f "${BRIDGE_PID_FILE}" ]]; then
    local old_pid
    old_pid="$(cat "${BRIDGE_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
      echo "Subagent tmux bridge already running (pid=${old_pid})." | tee -a "${RUN_DIR}/logs/supervisor.log"
      return 0
    fi
  fi

  echo "Starting subagent tmux bridge (log: ${BRIDGE_LOG})." | tee -a "${RUN_DIR}/logs/supervisor.log"
  "${BRIDGE_SCRIPT}" --run-dir "${RUN_DIR}" --poll-sec "${SUBAGENT_BRIDGE_POLL_SEC}" >> "${BRIDGE_LOG}" 2>&1 &
  echo "$!" > "${BRIDGE_PID_FILE}"
}

stop_subagent_bridge() {
  if [[ -f "${BRIDGE_PID_FILE}" ]]; then
    local pid
    pid="$(cat "${BRIDGE_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" >/dev/null 2>&1 || true
      wait "${pid}" 2>/dev/null || true
      echo "Stopped subagent tmux bridge (pid=${pid})." | tee -a "${RUN_DIR}/logs/supervisor.log"
    fi
    rm -f "${BRIDGE_PID_FILE}"
  fi
}

trap stop_subagent_bridge EXIT

{
  echo "# CTF Run Findings"
  echo
  echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Workspace: /workspace/challenge"
  echo
  echo "## Initial Prompt"
  cat "${PROMPT_FILE}" || true
  echo
  echo "## Notes"
} >> "${RUN_DIR}/findings.md"

echo "Supervisor starting. Logs: ${RUN_DIR}/logs/supervisor.log" | tee -a "${RUN_DIR}/logs/supervisor.log"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found; dropping to shell" | tee -a "${RUN_DIR}/logs/supervisor.log"
  exec bash
fi

if ! docker ps --format '{{.Names}}' | grep -q '^ctf-toolbox$'; then
  echo "ctf-toolbox container not running; dropping to shell" | tee -a "${RUN_DIR}/logs/supervisor.log"
  exec bash
fi

if ! docker exec ctf-toolbox bash -lc 'command -v codex >/dev/null 2>&1'; then
  echo "codex CLI not found in ctf-toolbox container." | tee -a "${RUN_DIR}/logs/supervisor.log"
  echo "Install it in the container and rerun supervisor." | tee -a "${RUN_DIR}/logs/supervisor.log"
  exec bash
fi

cat <<BANNER | tee -a "${RUN_DIR}/logs/supervisor.log"
========================================================
Codex supervisor launching in ctf-toolbox container.
Challenge dir: /workspace/challenge
Artifacts dir: /workspace/artifacts
Findings file: /workspace/findings.md
Injection queue: /workspace/inject.queue
Prompt file: /workspace/challenge_prompt.txt
========================================================
BANNER

echo "No worker windows are spawned by default." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Spawn subagents only if/when needed from inside this VM session." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$ctf-exploit-subagent" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$ctf-docs-subagent" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Subagent roles: exploit_tester, docs_researcher" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$webhook-site-callbacks" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Codex multi-agent mode is enabled; spawn subagents from supervisor when needed." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Spawned subagents are auto-mirrored to tmux sessions by subagent-tmux-bridge." | tee -a "${RUN_DIR}/logs/supervisor.log"

CODEX_AUTO_ALLOW="${CODEX_AUTO_ALLOW:-1}"
CODEX_ARGS=(codex --no-alt-screen --enable multi_agent)
if [[ "${CODEX_AUTO_ALLOW}" == "1" ]]; then
  # Avoid conflicting flags: do not combine --ask-for-approval with bypass mode.
  CODEX_ARGS+=(--ask-for-approval never --sandbox danger-full-access)
  echo "Codex auto-allow mode: enabled (--ask-for-approval never, --sandbox danger-full-access)." | tee -a "${RUN_DIR}/logs/supervisor.log"
else
  echo "Codex auto-allow mode: disabled (default Codex approval behavior)." | tee -a "${RUN_DIR}/logs/supervisor.log"
fi

start_subagent_bridge

echo "Launching interactive Codex session..." | tee -a "${RUN_DIR}/logs/supervisor.log"
set +e
initial_prompt="$(cat "${PROMPT_FILE}")"
docker exec -it ctf-toolbox bash -lc 'cd /workspace && "$@"' _ "${CODEX_ARGS[@]}" "${initial_prompt}"
rc=$?
set -e

stop_subagent_bridge
trap - EXIT

echo "Codex supervisor session exited with code ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "If this was unexpected (auth/session issue), run inside this shell:" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "  docker exec -it ctf-toolbox bash -lc 'cd /workspace && codex'" | tee -a "${RUN_DIR}/logs/supervisor.log"

exec bash
