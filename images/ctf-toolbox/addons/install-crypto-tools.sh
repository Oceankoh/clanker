#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must run as root." >&2
  echo "Example: docker exec -u root ctf-toolbox bash -lc '/workspace/ctf-toolbox/addons/install-crypto-tools.sh'" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "[crypto] Installing apt packages..."
apt-get update
apt-get install -y --no-install-recommends \
  hashcat \
  hcxtools \
  john \
  libimage-exiftool-perl \
  openssl \
  outguess \
  steghide

echo "[crypto] Installing python packages..."
uv pip install --system \
  pycryptodome \
  rsa

rm -rf /var/lib/apt/lists/*
echo "[crypto] Done."
