#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  echo "Example: docker exec -u root ctf-toolbox bash -lc '/workspace/ctf-toolbox/addons/install-heavy-ctf-tools.sh'" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[heavy] Installing apt packages..."
apt-get update
apt-get install -y --no-install-recommends \
  sagemath

# radare2 may be unavailable on some Debian mirrors/releases; best effort.
apt-get install -y --no-install-recommends radare2 || true

echo "[heavy] Installing python packages..."
uv pip install --system \
  angr

rm -rf /var/lib/apt/lists/*
echo "[heavy] Done."
