#!/usr/bin/env bash
# bake.sh — build the clanker golden VM image, once. Provisions a throwaway
# builder VM, runs install-toolbox.sh on it, snapshots it to a reusable image,
# records the metadata (so `clanker image status` shows the build date), then
# tears the builder down.
#
# ATTENDED ONLY: this spins up a real cloud VM. Run it yourself; do not automate
# it into an unattended path (see memory: live-smoke-test-must-be-attended).
#
#   images/golden/bake.sh --provider digitalocean     # the tested path
#   images/golden/bake.sh --provider gcp              # implemented, untested
#
# DigitalOcean needs: doctl (authenticated), an SSH key registered with DO
# (CTFVM_DO_SSH_KEY = fingerprint or name). GCP needs: gcloud (authenticated),
# CTFVM_GCP_PROJECT / CTFVM_GCP_ZONE.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
PROVIDER=""
VARIANT="${CTF_TOOLBOX_VARIANT:-lean}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
IMAGE_NAME="clanker-toolbox-${STAMP}"
DO_BASE_IMAGE="${CTFVM_DO_BASE_IMAGE:-ubuntu-24-04-x64}"
DO_SIZE="${CTFVM_DO_SIZE_SLUG:-s-4vcpu-8gb}"
DO_REGION="${CTFVM_DO_REGION:-nyc3}"
GCP_BASE_IMAGE_FAMILY="${CTFVM_GCP_BASE_IMAGE_FAMILY:-debian-12}"
GCP_MACHINE="${CTFVM_GCP_MACHINE_TYPE:-e2-standard-4}"

log() { echo "[bake] $*"; }
die() { echo "[bake] ERROR: $*" >&2; exit 1; }

# Builder identifiers live at global scope so the EXIT trap (which runs after the
# provider function returns) can still see them under `set -u`.
BUILDER_DO_ID=""
BUILDER_GCP_NAME=""
PAYLOAD=""
PAYLOAD_TAR=""
cleanup() {
  # Retry the destroy: a transient API/DNS blip must NOT leave a builder billing.
  if [ -n "${BUILDER_DO_ID}" ]; then
    log "cleanup: destroying builder ${BUILDER_DO_ID}"
    for _ in 1 2 3 4 5 6; do
      doctl compute droplet delete "${BUILDER_DO_ID}" -f >/dev/null 2>&1 && break
      log "  delete failed (API/DNS?) — retrying in 5s…"; sleep 5
    done
    doctl compute droplet get "${BUILDER_DO_ID}" >/dev/null 2>&1 \
      && log "  WARNING: builder ${BUILDER_DO_ID} may still exist — verify with: doctl compute droplet list"
  fi
  if [ -n "${BUILDER_GCP_NAME}" ]; then
    log "cleanup: deleting builder ${BUILDER_GCP_NAME}"
    for _ in 1 2 3 4 5 6; do
      gcloud compute instances delete "${BUILDER_GCP_NAME}" --project "${CTFVM_GCP_PROJECT:-}" --zone "${CTFVM_GCP_ZONE:-}" -q >/dev/null 2>&1 && break
      sleep 5
    done
  fi
  [ -n "${PAYLOAD}" ] && rm -rf "${PAYLOAD}" 2>/dev/null || true
  [ -n "${PAYLOAD_TAR}" ] && rm -f "${PAYLOAD_TAR}" 2>/dev/null || true
}
trap cleanup EXIT

record() {  # provider image_id image_name codex_ver claude_ver
  ( cd "${REPO_ROOT}" && PYTHONPATH="${REPO_ROOT}" python3 -m clanker image record \
      --provider "$1" --image-id "$2" --image-name "$3" \
      --built-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --codex-version "${4:-}" --claude-version "${5:-}" )
}

# Read CODEX_VERSION / CLAUDE_VERSION out of the versions file the install script
# wrote, given its contents on stdin.
parse_ver() { grep -E "^$1=" | head -n1 | cut -d= -f2- | tr -d '\r'; }

build_payload_tar() {
  PAYLOAD="$(mktemp -d)"
  cp "${HERE}/install-toolbox.sh" "${PAYLOAD}/install-toolbox.sh"
  mkdir -p "${PAYLOAD}/ctf-toolbox-payload"
  cp -r "${REPO_ROOT}/images/ctf-toolbox/mcp" "${PAYLOAD}/ctf-toolbox-payload/mcp" 2>/dev/null || true
  cp -r "${REPO_ROOT}/images/ctf-toolbox/codex-config" "${PAYLOAD}/ctf-toolbox-payload/codex-config" 2>/dev/null || true
  tar -C "${PAYLOAD}" -czf "${PAYLOAD}.tar.gz" .
  echo "${PAYLOAD}.tar.gz"
}

# --------------------------------------------------------------------------
# DigitalOcean
# --------------------------------------------------------------------------
bake_digitalocean() {
  command -v doctl >/dev/null || die "doctl not found / not authenticated"
  local ssh_key="${CTFVM_DO_SSH_KEY:-}"
  [ -n "${ssh_key}" ] || die "set CTFVM_DO_SSH_KEY to an SSH key name/fingerprint registered with DO"

  local payload; payload="$(build_payload_tar)"
  local name="ctfvm-golden-builder-${STAMP}"
  log "creating builder droplet ${name} (${DO_SIZE}, ${DO_REGION}, ${DO_BASE_IMAGE})…"
  local id
  id="$(doctl compute droplet create "${name}" --image "${DO_BASE_IMAGE}" \
        --size "${DO_SIZE}" --region "${DO_REGION}" --ssh-keys "${ssh_key}" \
        --wait --format ID --no-header)"
  [ -n "${id}" ] || die "droplet create failed"
  BUILDER_DO_ID="${id}"; PAYLOAD_TAR="${payload}"   # global, so the EXIT trap cleans up

  local ip
  ip="$(doctl compute droplet get "${id}" --format PublicIPv4 --no-header)"
  log "builder ip ${ip}; waiting for SSH…"
  # ServerAliveInterval/CountMax: if the connection stalls, ssh exits after
  # ~60s instead of hanging forever (the 3.5h-hang bug). ConnectTimeout bounds
  # the initial connect; BatchMode never waits on a password prompt.
  local sshopts="-o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4"
  local ssh="ssh ${sshopts} root@${ip}"
  local up=0
  for _ in $(seq 1 40); do ${ssh} true 2>/dev/null && { up=1; break; }; sleep 5; done
  [ "${up}" = "1" ] || die "builder never became reachable over SSH"

  log "uploading payload + running install-toolbox.sh (this takes a few minutes)…"
  scp ${sshopts} "${payload}" "root@${ip}:/tmp/payload.tar.gz"
  # `timeout` on the remote install caps the whole provision so a stuck apt can
  # never wedge the bake indefinitely.
  ${ssh} "set -e; mkdir -p /tmp/payload && tar -C /tmp/payload -xzf /tmp/payload.tar.gz \
          && cp -r /tmp/payload/ctf-toolbox-payload /tmp/ctf-toolbox-payload \
          && timeout 1500 env CTF_TOOLBOX_VARIANT='${VARIANT}' bash /tmp/payload/install-toolbox.sh"

  local versions codex_ver claude_ver
  versions="$(${ssh} cat /var/lib/ctfvm/golden-versions.env 2>/dev/null || true)"
  codex_ver="$(printf '%s\n' "${versions}" | parse_ver CODEX_VERSION)"
  claude_ver="$(printf '%s\n' "${versions}" | parse_ver CLAUDE_VERSION)"

  log "powering off + snapshotting as ${IMAGE_NAME}…"
  doctl compute droplet-action power-off "${id}" --wait
  doctl compute droplet-action snapshot "${id}" --snapshot-name "${IMAGE_NAME}" --wait

  local image_id
  image_id="$(doctl compute image list-user --format ID,Name --no-header | awk -v n="${IMAGE_NAME}" '$2==n{print $1; exit}')"
  [ -n "${image_id}" ] || die "could not find snapshot id for ${IMAGE_NAME}"

  record digitalocean "${image_id}" "${IMAGE_NAME}" "${codex_ver}" "${claude_ver}"
  log "done. Set CTFVM_GOLDEN_IMAGE_DO=${image_id} (or it's read from .ctfvm/golden-image.json)."
}

# --------------------------------------------------------------------------
# GCP (mirror of DO; implemented but untested by the maintainer)
# --------------------------------------------------------------------------
bake_gcp() {
  command -v gcloud >/dev/null || die "gcloud not found / not authenticated"
  local project="${CTFVM_GCP_PROJECT:-}" zone="${CTFVM_GCP_ZONE:-}"
  [ -n "${project}" ] && [ -n "${zone}" ] || die "set CTFVM_GCP_PROJECT and CTFVM_GCP_ZONE"

  local payload; payload="$(build_payload_tar)"
  local name="ctfvm-golden-builder-${STAMP}"
  log "creating builder instance ${name} (${GCP_MACHINE}, ${zone}, ${GCP_BASE_IMAGE_FAMILY})…"
  gcloud compute instances create "${name}" --project "${project}" --zone "${zone}" \
    --machine-type "${GCP_MACHINE}" --image-family "${GCP_BASE_IMAGE_FAMILY}" \
    --image-project debian-cloud
  BUILDER_GCP_NAME="${name}"; PAYLOAD_TAR="${payload}"   # global, so the EXIT trap cleans up

  log "waiting for SSH…"
  for _ in $(seq 1 30); do gcloud compute ssh "${name}" --project "${project}" --zone "${zone}" --command true 2>/dev/null && break; sleep 5; done

  log "uploading payload + running install-toolbox.sh…"
  gcloud compute scp "${payload}" "${name}:/tmp/payload.tar.gz" --project "${project}" --zone "${zone}"
  gcloud compute ssh "${name}" --project "${project}" --zone "${zone}" --command \
    "set -e; mkdir -p /tmp/payload && tar -C /tmp/payload -xzf /tmp/payload.tar.gz \
     && sudo cp -r /tmp/payload/ctf-toolbox-payload /tmp/ctf-toolbox-payload \
     && sudo timeout 1500 env CTF_TOOLBOX_VARIANT='${VARIANT}' bash /tmp/payload/install-toolbox.sh"

  local versions codex_ver claude_ver
  versions="$(gcloud compute ssh "${name}" --project "${project}" --zone "${zone}" --command 'sudo cat /var/lib/ctfvm/golden-versions.env' 2>/dev/null || true)"
  codex_ver="$(printf '%s\n' "${versions}" | parse_ver CODEX_VERSION)"
  claude_ver="$(printf '%s\n' "${versions}" | parse_ver CLAUDE_VERSION)"

  log "stopping instance + creating image ${IMAGE_NAME}…"
  gcloud compute instances stop "${name}" --project "${project}" --zone "${zone}"
  gcloud compute images create "${IMAGE_NAME}" --project "${project}" \
    --source-disk "${name}" --source-disk-zone "${zone}"

  record gcp "${IMAGE_NAME}" "${IMAGE_NAME}" "${codex_ver}" "${claude_ver}"
  log "done. Set CTFVM_GOLDEN_IMAGE_GCP=${IMAGE_NAME} (or it's read from .ctfvm/golden-image.json)."
}

# --------------------------------------------------------------------------
# args + dispatch
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --provider) PROVIDER="$2"; shift 2 ;;
    --variant)  VARIANT="$2";  shift 2 ;;
    *) die "unknown arg: $1" ;;
  esac
done
[ -n "${PROVIDER}" ] || die "usage: bake.sh --provider {digitalocean|gcp} [--variant lean|full]"

case "${PROVIDER}" in
  do|digitalocean|digital-ocean) bake_digitalocean ;;
  gcp|google) bake_gcp ;;
  *) die "unknown provider: ${PROVIDER}" ;;
esac
