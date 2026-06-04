#!/usr/bin/env bash
# Live end-to-end smoke test: provision a real VM, run an agent on one
# challenge, assert the flag lands in findings/the agent pane, then destroy the
# VM. This is the repeatable version of a manual smoke run — it spends a few
# cents and a few minutes, so it is opt-in (not part of `tests/`).
#
#   scripts/smoke.sh --agent claude-code --dir .ctf-work/smoke-challenges/01-strings
#   scripts/smoke.sh --agent codex       --dir .ctf-work/smoke-challenges/04-hidden
#
# Needs: a cloud CLI authed (doctl/gcloud) + the chosen agent authed
# (`python3 -m clanker auth ...` / `codex login`). Builds a local toolbox image
# archive on first run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

AGENT="claude-code"
DIR="${REPO_ROOT}/.ctf-work/smoke-challenges/01-strings"
FLAG=""
SIZE="s-2vcpu-4gb"
PROVIDER=""
TIMEOUT="600"
KEEP="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --flag) FLAG="$2"; shift 2 ;;
    --size|--size-slug) SIZE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --keep) KEEP="1"; shift ;;
    -h|--help)
      echo "usage: scripts/smoke.sh [--agent codex|claude-code] [--dir CHAL] [--flag flag{...}]"
      echo "                        [--size SLUG] [--provider gcp|digitalocean] [--timeout SEC] [--keep]"
      exit 0 ;;
    *) echo "[smoke] unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -d "${DIR}" ]] || { echo "[smoke] challenge dir not found: ${DIR}" >&2; exit 2; }
DIR="$(cd "${DIR}" && pwd)"
name="$(basename "${DIR}")"

# expected flag: --flag, else look up by challenge name in EXPECTED.tsv (kept at
# the folder root so it is never uploaded to the VM)
if [[ -z "${FLAG}" ]]; then
  exp="$(dirname "${DIR}")/EXPECTED.tsv"
  [[ -f "${exp}" ]] && FLAG="$(awk -F'\t' -v n="${name}" '$1==n{print $2}' "${exp}")"
fi
[[ -n "${FLAG}" ]] || { echo "[smoke] no expected flag for '${name}' (pass --flag or add EXPECTED.tsv)" >&2; exit 2; }

echo "[smoke] agent=${AGENT}  challenge=${name}  expect=${FLAG}"

# preflight: agent authenticated?
if ! ( cd "${REPO_ROOT}" && PYTHONPATH="${REPO_ROOT}" python3 -m clanker agents 2>/dev/null \
        | grep -E "^${AGENT}[[:space:]]" | grep -q "auth=ready" ); then
  echo "[smoke] FAIL: '${AGENT}' is not authenticated." >&2
  echo "        Fix: python3 -m clanker auth ${AGENT%%-*}   (codex: 'codex login' or set OPENAI_API_KEY)" >&2
  exit 1
fi

# toolbox image: build a local archive if none exists
ARCHIVE="${CTFVM_LOCAL_IMAGE_ARCHIVE:-${REPO_ROOT}/.ctfvm/cache/ctf-toolbox.tar.gz}"
if [[ ! -f "${ARCHIVE}" ]]; then
  echo "[smoke] no local toolbox image; building one (slow on first run)..."
  "${SCRIPT_DIR}/ctfvm" image build-local --variant lean
fi

echo "[smoke] provisioning (this takes a few minutes)..."
"${SCRIPT_DIR}/ctfvm" start --dir "${DIR}" --agent "${AGENT}" --use-local-image --no-vpn \
  --size-slug "${SIZE}" ${PROVIDER:+--provider "${PROVIDER}"}

RID="$(python3 -c "import json,sys;print(json.load(open('${REPO_ROOT}/.ctfvm/current-run.json')).get('run_id',''))" 2>/dev/null || true)"
[[ -n "${RID}" ]] || { echo "[smoke] FAIL: could not read run id from .ctfvm/current-run.json" >&2; exit 1; }

cleanup() {
  if [[ "${KEEP}" == "1" ]]; then
    echo "[smoke] --keep: leaving ${RID} alive (destroy: scripts/ctfvm destroy --run-id ${RID})"; return
  fi
  echo "[smoke] destroying ${RID}..."
  "${SCRIPT_DIR}/ctfvm" destroy --run-id "${RID}" >/dev/null 2>&1 \
    || echo "[smoke] WARN: destroy failed — run: scripts/ctfvm destroy --run-id ${RID}" >&2
}
trap cleanup EXIT

echo "[smoke] run ${RID} up; watching for the flag (timeout ${TIMEOUT}s)..."
read_output() {
  ( cd "${REPO_ROOT}" && PYTHONPATH="${REPO_ROOT}" python3 - "${RID}" <<'PY' 2>/dev/null
import sys
from clanker.server.service import UiService
try:
    s = UiService().snapshot(sys.argv[1], include_artifacts=False)
    print(s.findings_tail or "")
    for p in (s.panes or []):
        print(p.output or "")
except Exception:
    pass
PY
  )
}

found="0"; deadline=$(( SECONDS + TIMEOUT ))
while (( SECONDS < deadline )); do
  if read_output | grep -qF "${FLAG}"; then found="1"; break; fi
  sleep 15
done

if [[ "${found}" == "1" ]]; then
  echo "[smoke] PASS — ${AGENT} recovered ${FLAG} on ${name}"
  exit 0
fi
echo "[smoke] FAIL — flag not found within ${TIMEOUT}s (run ${RID})" >&2
exit 1
