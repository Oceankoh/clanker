#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage: install-common-addins.sh [profile...]

Profiles:
  crypto      Install common cryptography/password-cracking tools.
  cloud       Install common cloud/Kubernetes tooling.
  forensics   Install common DFIR/forensics tooling.
  pentest     Install pentest/web recon tools (ffuf, nmap, sqlmap).
  heavy       Install heavyweight CTF extras (sagemath, radare2, angr).
  all         Install default profiles (crypto, cloud, forensics, heavy).

Examples:
  ./install-common-addins.sh crypto
  ./install-common-addins.sh cloud forensics
  ./install-common-addins.sh all
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "$#" -eq 0 ]]; then
  usage
  exit 1
fi

declare -a profiles=()
for arg in "$@"; do
  case "$arg" in
    all)
      profiles+=(crypto cloud forensics heavy)
      ;;
    crypto|cloud|forensics|pentest|heavy)
      profiles+=("$arg")
      ;;
    *)
      echo "Unknown profile: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

run_profile() {
  local profile="$1"
  local script_path="${SCRIPT_DIR}/install-${profile}-tools.sh"
  if [[ ! -x "$script_path" ]]; then
    echo "Missing installer script: $script_path" >&2
    exit 1
  fi
  echo "[addins] Running ${profile} profile..."
  "$script_path"
}

seen=" "
for profile in "${profiles[@]}"; do
  if [[ "$seen" == *" $profile "* ]]; then
    continue
  fi
  run_profile "$profile"
  seen="${seen}${profile} "
done

echo "[addins] Completed requested profile installs."
