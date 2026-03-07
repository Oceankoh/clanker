#!/usr/bin/env bash
set -euo pipefail

RUN_DIR="/home/ctf/run"
IDEAS_INLINE=""
IDEAS_FILE=""
PARALLEL=""
MODEL=""

usage() {
  cat <<USAGE
Usage:
  spawn-idea-workers.sh [--run-dir /home/ctf/run] (--ideas "idea 1; idea 2" | --ideas-file /home/ctf/run/ideas.txt) [--parallel <n>] [--model gpt-5.3-codex]

Notes:
  - Spawns one tmux window per idea in session "ctf".
  - Window names are idea-001, idea-002, ...
  - --parallel is accepted for compatibility and currently ignored in tmux mode.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-dir)
      RUN_DIR="$2"; shift 2 ;;
    --ideas)
      IDEAS_INLINE="$2"; shift 2 ;;
    --ideas-file)
      IDEAS_FILE="$2"; shift 2 ;;
    --parallel)
      PARALLEL="$2"; shift 2 ;;
    --model)
      MODEL="$2"; shift 2 ;;
    -h|--help)
      usage
      exit 0 ;;
    *)
      echo "Unknown arg: $1" >&2
      usage >&2
      exit 1 ;;
  esac
done

if [[ -n "${IDEAS_INLINE}" && -n "${IDEAS_FILE}" ]]; then
  echo "Use either --ideas or --ideas-file, not both." >&2
  exit 1
fi
if [[ -z "${IDEAS_INLINE}" && -z "${IDEAS_FILE}" ]]; then
  echo "Missing required input: --ideas or --ideas-file." >&2
  exit 1
fi
if [[ -n "${IDEAS_FILE}" && ! -f "${IDEAS_FILE}" ]]; then
  echo "Ideas file not found: ${IDEAS_FILE}" >&2
  exit 1
fi
if [[ -n "${PARALLEL}" && ( ! "${PARALLEL}" =~ ^[0-9]+$ || "${PARALLEL}" -lt 1 ) ]]; then
  echo "Invalid --parallel value: ${PARALLEL} (must be >= 1)." >&2
  exit 1
fi
if [[ -n "${MODEL}" && ! "${MODEL}" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "Invalid --model value: ${MODEL}" >&2
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
if ! tmux has-session -t ctf 2>/dev/null; then
  echo "No tmux session named ctf found." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker not found." >&2
  exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -q '^ctf-toolbox$'; then
  echo "ctf-toolbox container not running." >&2
  exit 1
fi

mkdir -p "${RUN_DIR}/logs" "${RUN_DIR}/artifacts/ideas"

collect_ideas() {
  if [[ -n "${IDEAS_FILE}" ]]; then
    cat "${IDEAS_FILE}"
  else
    printf '%s\n' "${IDEAS_INLINE}" | tr ';' '\n'
  fi
}

slugify() {
  local text="$1"
  local slug
  slug="$(printf '%s' "${text}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//; s/-{2,}/-/g')"
  if [[ -z "${slug}" ]]; then
    slug="idea"
  fi
  if (( ${#slug} > 36 )); then
    slug="${slug:0:36}"
    slug="${slug%-}"
  fi
  printf '%s\n' "${slug}"
}

worker_prompt() {
  local idea="$1"
  local worker_id="$2"
  local template_file=""
  local esc_idea esc_worker

  if [[ -f "${RUN_DIR}/prompts/workers/idea_worker.txt" ]]; then
    template_file="${RUN_DIR}/prompts/workers/idea_worker.txt"
  elif [[ -f "/workspace/prompts/workers/idea_worker.txt" ]]; then
    template_file="/workspace/prompts/workers/idea_worker.txt"
  elif [[ -f "/home/ctf/run/prompts/workers/idea_worker.txt" ]]; then
    template_file="/home/ctf/run/prompts/workers/idea_worker.txt"
  fi

  esc_idea="$(printf '%s' "${idea}" | sed -e 's/[&|]/\\&/g')"
  esc_worker="$(printf '%s' "${worker_id}" | sed -e 's/[&|]/\\&/g')"

  if [[ -n "${template_file}" ]]; then
    sed \
      -e "s|__IDEA__|${esc_idea}|g" \
      -e "s|__WORKER_ID__|${esc_worker}|g" \
      "${template_file}"
    return 0
  fi

  cat <<PROMPT
You are a focused CTF worker exploring one exploitation hypothesis.

Hypothesis to test:
${idea}

Workspace:
- Challenge input: /workspace/challenge
- Shared findings: /workspace/findings.md
- Your artifact dir: /workspace/artifacts/ideas/\${WORKER_ID}

Rules:
- Prioritize quick falsification/validation of this hypothesis.
- Save reproducible commands/scripts in your artifact directory.
- Append a short findings entry tagged [idea:\${WORKER_ID}] to /workspace/findings.md.
- If the hypothesis fails, propose up to 3 concrete next hypotheses.

Start by writing a 3-6 step plan, then execute.
PROMPT
}

spawn_worker_window() {
  local idx="$1"
  local idea="$2"

  local idx_pad slug worker_id window_name
  local worker_dir prompt_file log_file worker_script

  idx_pad="$(printf '%03d' "${idx}")"
  slug="$(slugify "${idea}")"
  worker_id="idea-${idx_pad}-${slug}"
  window_name="idea-${idx_pad}"

  worker_dir="${RUN_DIR}/artifacts/ideas/${worker_id}"
  prompt_file="${RUN_DIR}/worker-${worker_id}.prompt.txt"
  log_file="${RUN_DIR}/logs/worker-${worker_id}.log"
  worker_script="${RUN_DIR}/run-worker-${worker_id}.sh"

  mkdir -p "${worker_dir}"
  worker_prompt "${idea}" "${worker_id}" > "${prompt_file}"
  chmod 640 "${prompt_file}" 2>/dev/null || true

  cat > "${worker_script}" <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail
echo "[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting ${window_name} (${worker_id})" | tee -a "${log_file}"
CODEX_AUTO_ALLOW="\${CODEX_AUTO_ALLOW:-1}"
CODEX_ARGS=(codex --no-alt-screen)
if [[ "\${CODEX_AUTO_ALLOW}" == "1" ]]; then
  CODEX_ARGS+=(--ask-for-approval never --sandbox danger-full-access)
fi
if [[ -n "${MODEL}" ]]; then
  CODEX_ARGS+=(--model "${MODEL}")
fi
docker exec -it ctf-toolbox bash -lc 'cd /workspace && prompt="\$(cat "/workspace/worker-${worker_id}.prompt.txt")" && "\$@" "\$prompt"' _ "\${CODEX_ARGS[@]}" | tee -a "${log_file}"
rc=\${PIPESTATUS[0]}
echo "[\$(date -u +%Y-%m-%dT%H:%M:%SZ)] ${window_name} exited rc=\${rc}" | tee -a "${log_file}"
exit "\${rc}"
SCRIPT
  chmod +x "${worker_script}"

  tmux list-windows -t ctf -F '#{window_name}' | grep -qx "${window_name}" && tmux kill-window -t "ctf:${window_name}" || true
  tmux new-window -d -t ctf -n "${window_name}" "bash -lc '${worker_script}; echo; echo ${window_name} complete; exec bash'"
  echo "Spawned ${window_name} for ${worker_id}"
}

IDEA_LINES=()
while IFS= read -r line; do
  line="$(printf '%s' "${line}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
  [[ -z "${line}" ]] && continue
  [[ "${line}" =~ ^# ]] && continue
  IDEA_LINES+=("${line}")
done < <(collect_ideas)

if [[ "${#IDEA_LINES[@]}" -eq 0 ]]; then
  echo "No non-empty ideas found." >&2
  exit 1
fi

if [[ -n "${PARALLEL}" ]]; then
  echo "Note: --parallel is currently ignored in tmux mode; spawning one window per idea."
fi

i=1
for idea in "${IDEA_LINES[@]}"; do
  spawn_worker_window "${i}" "${idea}"
  i=$((i + 1))
done

echo "Spawned ${#IDEA_LINES[@]} idea worker window(s). Attach with: tmux attach -t ctf"
