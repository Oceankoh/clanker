#!/usr/bin/env bash
set -euo pipefail

variant="${CTF_TOOLBOX_VARIANT:-lean}"
venv_path="${VIRTUAL_ENV:-/workspace/.venv}"
home_dir="${HOME:-/workspace}"
marker_dir="${home_dir}/.cache/ctf-toolbox"
marker_file="${marker_dir}/python-runtime-${variant}.done"

mkdir -p "${marker_dir}"

if [[ -f "${marker_file}" ]]; then
  echo "[python-runtime] Python tooling already initialized for variant '${variant}'."
  exit 0
fi

echo "[python-runtime] Initializing venv at ${venv_path}..."

if command -v uv >/dev/null 2>&1; then
  uv venv "${venv_path}"
  uv pip install --python "${venv_path}/bin/python" pwntools z3-solver
  if [[ "${variant}" == "full" ]]; then
    uv pip install --python "${venv_path}/bin/python" angr
  fi
else
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[python-runtime] python3 is required but not found in container." >&2
    exit 1
  fi
  python3 -m venv "${venv_path}"
  "${venv_path}/bin/python" -m pip install --upgrade pip
  "${venv_path}/bin/python" -m pip install pwntools z3-solver
  if [[ "${variant}" == "full" ]]; then
    "${venv_path}/bin/python" -m pip install angr
  fi
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "${marker_file}"
echo "[python-runtime] Completed."
