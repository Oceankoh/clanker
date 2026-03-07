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
echo "Supervisor skill available: \$ctf-idea-workers" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Supervisor skill available: \$webhook-site-callbacks" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "Idea-worker helper (tmux windows): /home/ctf/run/spawn-idea-workers.sh --run-dir /home/ctf/run --ideas 'idea1;idea2'" | tee -a "${RUN_DIR}/logs/supervisor.log"

CODEX_AUTO_ALLOW="${CODEX_AUTO_ALLOW:-1}"
CODEX_ARGS=(codex --no-alt-screen)
if [[ "${CODEX_AUTO_ALLOW}" == "1" ]]; then
  # Avoid conflicting flags: do not combine --ask-for-approval with bypass mode.
  CODEX_ARGS+=(--ask-for-approval never --sandbox danger-full-access)
  echo "Codex auto-allow mode: enabled (--ask-for-approval never, --sandbox danger-full-access)." | tee -a "${RUN_DIR}/logs/supervisor.log"
else
  echo "Codex auto-allow mode: disabled (default Codex approval behavior)." | tee -a "${RUN_DIR}/logs/supervisor.log"
fi

echo "Launching interactive Codex session..." | tee -a "${RUN_DIR}/logs/supervisor.log"
set +e
initial_prompt="$(cat "${PROMPT_FILE}")"
docker exec -it ctf-toolbox bash -lc 'cd /workspace && "$@"' _ "${CODEX_ARGS[@]}" "${initial_prompt}"
rc=$?
set -e

echo "Codex supervisor session exited with code ${rc} at $(date -u +%Y-%m-%dT%H:%M:%SZ)." | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "If this was unexpected (auth/session issue), run inside this shell:" | tee -a "${RUN_DIR}/logs/supervisor.log"
echo "  docker exec -it ctf-toolbox bash -lc 'cd /workspace && codex'" | tee -a "${RUN_DIR}/logs/supervisor.log"

exec bash
