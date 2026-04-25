#!/usr/bin/env bash

ctfvm_provider_normalize() {
  local provider="${1:-}"
  provider="$(printf '%s' "${provider}" | tr '[:upper:]' '[:lower:]')"
  case "${provider}" in
    ""|gcp|google|google-cloud|googlecloud)
      printf '%s\n' "gcp" ;;
    do|digitalocean|digital-ocean)
      printf '%s\n' "digitalocean" ;;
    *)
      echo "Unsupported provider: ${1}" >&2
      return 1 ;;
  esac
}

ctfvm_provider_label() {
  case "$(ctfvm_provider_normalize "$1")" in
    gcp) printf '%s\n' "GCP" ;;
    digitalocean) printf '%s\n' "DigitalOcean" ;;
  esac
}

ctfvm_provider_location_label() {
  case "$(ctfvm_provider_normalize "$1")" in
    gcp) printf '%s\n' "Zone" ;;
    digitalocean) printf '%s\n' "Region" ;;
  esac
}

ctfvm_provider_scope_label() {
  case "$(ctfvm_provider_normalize "$1")" in
    gcp) printf '%s\n' "Project" ;;
    digitalocean) printf '%s\n' "Account" ;;
  esac
}

ctfvm_provider_call() {
  local provider fn
  provider="$(ctfvm_provider_normalize "$1")" || return 1
  shift
  fn="ctfvm_provider_${provider}_$1"
  shift
  if ! declare -F "${fn}" >/dev/null 2>&1; then
    echo "Provider function not implemented: ${fn}" >&2
    return 1
  fi
  "${fn}" "$@"
}

ctfvm_provider_list_running_all() {
  local provider
  for provider in gcp digitalocean; do
    ctfvm_provider_call "${provider}" list_running || true
  done
}

SCRIPT_PROVIDER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "${SCRIPT_PROVIDER_DIR}/providers/gcp.sh"
. "${SCRIPT_PROVIDER_DIR}/providers/digitalocean.sh"
