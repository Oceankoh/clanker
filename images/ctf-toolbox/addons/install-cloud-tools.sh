#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  echo "Example: docker exec -u root ctf-toolbox bash -lc '/workspace/ctf-toolbox/addons/install-cloud-tools.sh'" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[cloud] Installing apt packages..."
apt-get update
apt-get install -y --no-install-recommends \
  awscli \
  kubernetes-client

echo "[cloud] Installing python packages..."
uv pip install --system \
  scoutsuite \
  trufflehog

rm -rf /var/lib/apt/lists/*
echo "[cloud] Done."
