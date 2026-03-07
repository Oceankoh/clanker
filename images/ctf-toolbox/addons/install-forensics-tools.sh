#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  echo "Example: docker exec -u root ctf-toolbox bash -lc '/workspace/ctf-toolbox/addons/install-forensics-tools.sh'" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[forensics] Installing apt packages..."
apt-get update
apt-get install -y --no-install-recommends \
  binwalk \
  exiftool \
  foremost \
  sleuthkit \
  testdisk \
  tshark \
  yara

echo "[forensics] Installing python packages..."
uv pip install --system \
  volatility3

rm -rf /var/lib/apt/lists/*
echo "[forensics] Done."
