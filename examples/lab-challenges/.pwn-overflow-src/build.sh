#!/usr/bin/env bash
# Rebuild the pwn-overflow binary as a linux/amd64 ELF (matches the CTF VM),
# using a docker gcc image so it works from any host (incl. Apple Silicon).
# Output lands in ../pwn-overflow/vuln (what the agent receives).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${HERE}/../pwn-overflow/vuln"

docker run --rm --platform linux/amd64 -v "${HERE}":/src -w /src gcc:13 \
  gcc -fno-stack-protector -no-pie -O0 -o /src/vuln vuln.c

# docker writes as root — normalize into place with our ownership
cat "${HERE}/vuln" > "${OUT}"
chmod 755 "${OUT}"
rm -f "${HERE}/vuln"
echo "built ${OUT}"
file "${OUT}" || true
