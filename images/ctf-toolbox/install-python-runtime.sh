#!/usr/bin/env bash
set -euo pipefail

variant="${CTF_TOOLBOX_VARIANT:-lean}"
venv_path="${VIRTUAL_ENV:-/home/ctf/.venv}"
marker_dir="${HOME}/.cache/ctf-toolbox"
marker_file="${marker_dir}/python-runtime-${variant}.done"

mkdir -p "${marker_dir}"

if [[ -f "${marker_file}" ]]; then
  echo "[python-runtime] Python tooling already initialized for variant '${variant}'."
  exit 0
fi

echo "[python-runtime] Creating virtualenv at ${venv_path}..."
uv venv "${venv_path}"

echo "[python-runtime] Installing base packages..."
uv pip install --python "${venv_path}/bin/python" \
  pwntools \
  z3-solver

if [[ "${variant}" == "full" ]]; then
  echo "[python-runtime] Installing full-profile package(s)..."
  uv pip install --python "${venv_path}/bin/python" angr
fi

date -u +%Y-%m-%dT%H:%M:%SZ > "${marker_file}"
echo "[python-runtime] Completed."
