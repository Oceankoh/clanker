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

  # Namespace subagent sessions per challenge so multiple challenges on one worker
  # don't collide (and the snapshot can filter by "subagent-<slug>"). The legacy
  # single-run workspace keeps the bare "subagent" prefix.
  local bridge_prefix="subagent"
  local base; base="$(basename "${RUN_DIR}")"
  if [[ "${RUN_DIR}" != "/home/ctf/run" && -n "${base}" ]]; then
    bridge_prefix="subagent-${base}"
  fi
  echo "Starting subagent tmux bridge (log: ${BRIDGE_LOG}, prefix: ${bridge_prefix})." | tee -a "${RUN_DIR}/logs/supervisor.log"
  "${BRIDGE_SCRIPT}" --run-dir "${RUN_DIR}" --poll-sec "${SUBAGENT_BRIDGE_POLL_SEC}" --session-prefix "${bridge_prefix}" >> "${BRIDGE_LOG}" 2>&1 &
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
  echo "Workspace: ${RUN_DIR}/challenge"
  echo
  echo "## Initial Prompt"
  cat "${PROMPT_FILE}" || true
  echo
  echo "## Notes"
} >> "${RUN_DIR}/findings.md"

echo "Supervisor starting. Logs: ${RUN_DIR}/logs/supervisor.log" | tee -a "${RUN_DIR}/logs/supervisor.log"

# Runtime mode: use the ctf-toolbox container when it's present (legacy Docker
# path), otherwise run the agent directly on the host (golden-image / worker
# path — Docker is being retired; see docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md).
USE_DOCKER=0
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ctf-toolbox$'; then
  USE_DOCKER=1
  echo "Runtime: ctf-toolbox container (Docker)." | tee -a "${RUN_DIR}/logs/supervisor.log"
else
  echo "Runtime: direct host (no Docker)." | tee -a "${RUN_DIR}/logs/supervisor.log"
fi

# --- agent backend selection (backend-aware; defaults to Codex) -------------
# `clanker stage-agent` writes ${RUN_DIR}/agent/{backend,launch.cmd,container.env}.
# When that dir is absent (older runs), we fall back to the historical Codex
# defaults so nothing changes for existing runs.
AGENT_DIR="${RUN_DIR}/agent"
AGENT_BACKEND="codex"
if [[ -f "${AGENT_DIR}/backend" ]]; then
  AGENT_BACKEND="$(tr -d '[:space:]' < "${AGENT_DIR}/backend" 2>/dev/null || echo codex)"
fi

# Default argv PER BACKEND, then override from launch.cmd if it parses to a
# non-empty command. The default is only a guard against a missing/empty
# launch.cmd (a partial stage); it follows AGENT_BACKEND so a Claude run doesn't
# silently launch Codex while the logs claim Claude.
CODEX_AUTO_ALLOW="${CODEX_AUTO_ALLOW:-1}"
case "${AGENT_BACKEND}" in
  claude*)
    AGENT_ARGS=(claude)
    [[ "${CODEX_AUTO_ALLOW}" == "1" ]] && AGENT_ARGS+=(--dangerously-skip-permissions)
    ;;
  *)
    AGENT_ARGS=(codex --no-alt-screen --enable multi_agent)
    [[ "${CODEX_AUTO_ALLOW}" == "1" ]] && AGENT_ARGS+=(--ask-for-approval never --sandbox danger-full-access)
    ;;
esac
if [[ -f "${AGENT_DIR}/launch.cmd" ]]; then
  read -r -a _staged_args < "${AGENT_DIR}/launch.cmd" || true
  if [[ ${#_staged_args[@]} -gt 0 ]]; then
    AGENT_ARGS=("${_staged_args[@]}")
  else
    echo "agent/launch.cmd is empty; falling back to ${AGENT_BACKEND} defaults." | tee -a "${RUN_DIR}/logs/supervisor.log"
  fi
fi
AGENT_BIN="${AGENT_ARGS[0]:-codex}"

# Per-agent env (e.g. CLAUDE_CODE_OAUTH_TOKEN), staged as KEY=VALUE. Injected
# per-challenge at launch — never baked into the image. Built in both forms:
# docker `-e` flags, and a host env array for the direct-host path.
ENV_FLAGS=()
HOST_ENV=()
if [[ -f "${AGENT_DIR}/container.env" ]]; then
  while IFS= read -r env_line; do
    [[ -z "${env_line}" || "${env_line}" == \#* ]] && continue
    ENV_FLAGS+=(-e "${env_line}")
    # The staged env was authored for the container (HOME=/workspace). On the
    # host the workspace IS the run dir, so remap /workspace -> RUN_DIR (fixes
    # e.g. CODEX_HOME=/workspace/.codex pointing at a path that doesn't exist).
    HOST_ENV+=("${env_line//\/workspace/${RUN_DIR}}")
  done < "${AGENT_DIR}/container.env"
fi

# On the host path, the agent CLIs live under the golden-image npm prefix.
HOST_NPM_BIN="${CTFVM_NPM_PREFIX:-/opt/ctfvm/npm-global}/bin"
if [[ "${USE_DOCKER}" == "1" ]]; then
  if ! docker exec ctf-toolbox bash -c 'command -v "$1" >/dev/null 2>&1' _ "${AGENT_BIN}"; then
    echo "${AGENT_BIN} CLI not found in ctf-toolbox container." | tee -a "${RUN_DIR}/logs/supervisor.log"
    exec bash
  fi
else
  export PATH="${RUN_DIR}/.venv/bin:${HOST_NPM_BIN}:${PATH}"
  if ! command -v "${AGENT_BIN}" >/dev/null 2>&1; then
    echo "${AGENT_BIN} CLI not found on host (looked in ${HOST_NPM_BIN})." | tee -a "${RUN_DIR}/logs/supervisor.log"
    echo "Rebuild the golden image (clanker image bake) or install the agent CLI." | tee -a "${RUN_DIR}/logs/supervisor.log"
    exec bash
  fi
fi

RUNTIME_LABEL="direct host (no Docker)"; [[ "${USE_DOCKER}" == "1" ]] && RUNTIME_LABEL="ctf-toolbox container"
cat <<BANNER | tee -a "${RUN_DIR}/logs/supervisor.log"
========================================================
${AGENT_BACKEND} supervisor launching (${RUNTIME_LABEL}).
Challenge dir: ${RUN_DIR}/challenge
Artifacts dir: ${RUN_DIR}/artifacts
Findings file: ${RUN_DIR}/findings.md
Injection queue: ${RUN_DIR}/inject.queue
Prompt file: ${RUN_DIR}/challenge_prompt.txt
========================================================
BANNER

echo "No worker windows are spawned by default." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Spawn subagents only if/when needed from inside this VM session." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$ctf-exploit-subagent" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$ctf-docs-subagent" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$gdb-mcp" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Subagent roles: exploit_tester, docs_researcher" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$webhook-site-callbacks" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Bundled MCP server available: gdb" | tee -a "${RUN_DIR}/logs/supervisor.log"
if [[ "${AGENT_BACKEND}" == codex* ]]; then
  echo "Codex multi-agent mode is enabled; spawn subagents from supervisor when needed." | tee -a "${RUN_DIR}/logs/supervisor.log"
fi
echo "Spawned subagents are auto-mirrored to tmux sessions by subagent-tmux-bridge." | tee -a "${RUN_DIR}/logs/supervisor.log"

echo "Agent backend: ${AGENT_BACKEND} (launch: ${AGENT_ARGS[*]})." | tee -a "${RUN_DIR}/logs/supervisor.log"

start_subagent_bridge

echo "Launching interactive ${AGENT_BACKEND} session..." | tee -a "${RUN_DIR}/logs/supervisor.log"
set +e
initial_prompt="$(cat "${PROMPT_FILE}")"
if [[ "${USE_DOCKER}" == "1" ]]; then
  docker exec -it ${ENV_FLAGS[@]+"${ENV_FLAGS[@]}"} ctf-toolbox bash -c 'cd /workspace && "$@"' _ "${AGENT_ARGS[@]}" "${initial_prompt}"
  rc=$?
else
  # Direct host: HOME=RUN_DIR so the agent finds its staged .codex/.claude config;
  # env carries the per-challenge credentials.
  ( cd "${RUN_DIR}" && HOME="${RUN_DIR}" env ${HOST_ENV[@]+"${HOST_ENV[@]}"} "${AGENT_ARGS[@]}" "${initial_prompt}" )
  rc=$?
fi
set -e

stop_subagent_bridge
trap - EXIT

echo "${AGENT_BACKEND} supervisor session exited with code ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)." | tee -a "${RUN_DIR}/logs/supervisor.log"
if [[ "${USE_DOCKER}" == "1" ]]; then
  echo "If unexpected, debug with:  docker exec -it ctf-toolbox bash -c 'cd /workspace && ${AGENT_BIN}'" | tee -a "${RUN_DIR}/logs/supervisor.log"
else
  echo "If unexpected, debug with:  cd ${RUN_DIR} && HOME=${RUN_DIR} ${AGENT_BIN}" | tee -a "${RUN_DIR}/logs/supervisor.log"
fi

exec bash
