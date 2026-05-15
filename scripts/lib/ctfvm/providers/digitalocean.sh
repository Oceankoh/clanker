#!/usr/bin/env bash

ctfvm_doctl() {
  local retry_max="${CTFVM_DOCTL_HTTP_RETRY_MAX:-0}"
  doctl --http-retry-max "${retry_max}" "$@"
}

ctfvm_provider_digitalocean_default_project() {
  if [[ -n "${CTFVM_DO_PROJECT:-}" ]]; then
    printf '%s\n' "${CTFVM_DO_PROJECT}"
  else
    printf '%s\n' "digitalocean"
  fi
}

ctfvm_provider_digitalocean_require_start_prereqs() {
  require_cmd doctl
}

ctfvm_provider_digitalocean_require_registry_prereqs() {
  require_cmd doctl
}

ctfvm_provider_digitalocean_default_location() {
  if [[ -n "${CTFVM_DO_REGION:-}" ]]; then
    printf '%s\n' "${CTFVM_DO_REGION}"
  elif [[ -n "${DIGITALOCEAN_REGION:-}" ]]; then
    printf '%s\n' "${DIGITALOCEAN_REGION}"
  elif [[ -n "${DO_REGION:-}" ]]; then
    printf '%s\n' "${DO_REGION}"
  else
    printf '%s\n' ""
  fi
}

ctfvm_provider_digitalocean_default_scope() {
  ctfvm_provider_digitalocean_default_project
}

ctfvm_provider_digitalocean_default_registry_location() {
  ctfvm_provider_digitalocean_default_location
}

ctfvm_provider_digitalocean_default_registry_repository() {
  ctfvm_doctl registries get --format Name --no-header 2>/dev/null | head -n1 || true
}

ctfvm_provider_digitalocean_get_field() {
  local instance="$1"
  local field="$2"
  ctfvm_doctl compute droplet get "${instance}" --format "${field}" --no-header 2>/dev/null | head -n1 || true
}

ctfvm_provider_digitalocean_list_running() {
  if ! command -v doctl >/dev/null 2>&1; then
    return 0
  fi
  local rows line name region status ip
  rows="$(ctfvm_doctl compute droplet list --tag-name ctfvm --format Name,Region,Status,PublicIPv4 --no-header 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    name="$(awk '{print $1}' <<< "${line}")"
    region="$(awk '{print $2}' <<< "${line}")"
    status="$(awk '{print $3}' <<< "${line}")"
    ip="$(awk '{print $4}' <<< "${line}")"
    [[ "${name}" == ctfvm-* ]] || continue
    [[ "${status}" == "active" ]] || continue
    printf 'digitalocean\t%s\t%s\t%s\t%s\t%s\n' "${name}" "${region}" "$(ctfvm_provider_digitalocean_default_project)" "${status}" "${ip}"
  done <<< "${rows}"
}

ctfvm_provider_digitalocean_get_status() {
  ctfvm_provider_digitalocean_get_field "$1" "Status"
}

ctfvm_provider_digitalocean_get_ip() {
  ctfvm_provider_digitalocean_get_field "$1" "PublicIPv4"
}

ctfvm_provider_digitalocean_get_region() {
  ctfvm_provider_digitalocean_get_field "$1" "Region"
}

ctfvm_provider_digitalocean_state_dir() {
  if [[ -n "${STATE_DIR:-}" ]]; then
    printf '%s\n' "${STATE_DIR}"
  else
    printf '%s\n' "${HOME}/.ctfvm"
  fi
}

ctfvm_provider_digitalocean_known_hosts_file() {
  local state_dir file
  state_dir="$(ctfvm_provider_digitalocean_state_dir)"
  mkdir -p "${state_dir}"
  file="${state_dir}/ssh_known_hosts"
  touch "${file}"
  chmod 600 "${file}" 2>/dev/null || true
  printf '%s\n' "${file}"
}

ctfvm_provider_digitalocean_ssh_alias() {
  local instance="$1"
  local ip="$2"
  if [[ -n "${instance}" ]]; then
    printf '%s\n' "ctfvm-do-${instance}"
    return 0
  fi
  printf '%s\n' "ctfvm-do-${ip//[^A-Za-z0-9_.-]/-}"
}

ctfvm_provider_digitalocean_ssh_common_args() {
  local instance="$1"
  local ip="$2"
  local known_hosts host_key_alias
  known_hosts="$(ctfvm_provider_digitalocean_known_hosts_file)"
  host_key_alias="$(ctfvm_provider_digitalocean_ssh_alias "${instance}" "${ip}")"
  printf '%s\n' -o "StrictHostKeyChecking=accept-new"
  printf '%s\n' -o "UserKnownHostsFile=${known_hosts}"
  printf '%s\n' -o "GlobalKnownHostsFile=/dev/null"
  printf '%s\n' -o "HostKeyAlias=${host_key_alias}"
}

ctfvm_provider_digitalocean_resolve_ssh_keys() {
  local -a keys=("$@")
  if [[ "${#keys[@]}" -gt 0 ]]; then
    printf '%s\n' "$(IFS=,; echo "${keys[*]}")"
    return 0
  fi

  if [[ -n "${CTFVM_DO_SSH_KEYS:-}" ]]; then
    printf '%s\n' "${CTFVM_DO_SSH_KEYS}"
    return 0
  fi
  if [[ -n "${CTFVM_DO_SSH_KEY:-}" ]]; then
    printf '%s\n' "${CTFVM_DO_SSH_KEY}"
    return 0
  fi

  ctfvm_doctl compute ssh-key list --format FingerPrint --no-header 2>/dev/null \
    | awk 'NF { print $1 }' \
    | paste -sd, -
}

ctfvm_provider_digitalocean_ssh_exec() {
  local instance="$1"
  local _zone="$2"
  local _project="$3"
  local ip="$4"
  local cmd="$5"
  local -a ssh_args=(ssh)
  local arg
  while IFS= read -r arg; do
    ssh_args+=("${arg}")
  done < <(ctfvm_provider_digitalocean_ssh_common_args "${instance}" "${ip}")
  ssh_args+=("root@${ip}" "${cmd}")
  "${ssh_args[@]}"
}

ctfvm_provider_digitalocean_ssh_tty() {
  local instance="$1"
  local _zone="$2"
  local _project="$3"
  local ip="$4"
  local cmd="$5"
  local -a ssh_args=(ssh -t)
  local arg
  while IFS= read -r arg; do
    ssh_args+=("${arg}")
  done < <(ctfvm_provider_digitalocean_ssh_common_args "${instance}" "${ip}")
  ssh_args+=("root@${ip}" "${cmd}")
  "${ssh_args[@]}"
}

ctfvm_provider_digitalocean_ssh_argv() {
  local instance="$1"
  local _zone="$2"
  local _project="$3"
  local ip="$4"
  shift 4

  local arg
  printf '%s\n' ssh
  while IFS= read -r arg; do
    printf '%s\n' "${arg}"
  done < <(ctfvm_provider_digitalocean_ssh_common_args "${instance}" "${ip}")
  printf '%s\n' "$@"
  printf '%s\n' "root@${ip}"
}

ctfvm_provider_digitalocean_create_instance() {
  local instance="$1"
  local zone="$2"
  local _project="$3"
  local size_slug="$4"
  local startup_script="$5"
  local boot_image="$6"
  local ssh_keys="$7"
  local tags="${CTFVM_DO_TAGS:-ctfvm,ctfvm-control}"
  local vpc_uuid="${CTFVM_DO_VPC_ID:-}"
  local -a args=(
    compute droplet create "${instance}"
    --region "${zone}"
    --size "${size_slug}"
    --image "${boot_image}"
    --user-data-file "${startup_script}"
    --tag-names "${tags}"
  )
  if [[ -n "${vpc_uuid}" ]]; then
    args+=(--vpc-uuid "${vpc_uuid}")
  fi
  if [[ -n "${ssh_keys}" ]]; then
    args+=(--ssh-keys "${ssh_keys}")
  fi

  ctfvm_doctl "${args[@]}" >/dev/null
}

ctfvm_provider_digitalocean_prepare_vpn_ingress() {
  local _instance="$1"
  local _zone="$2"
  local _project="$3"
  local _port="$4"
  # DigitalOcean droplets are reachable on their public interface unless the
  # user attaches a separate cloud firewall. In that case, allow this UDP port
  # on the user's managed firewall/tag policy.
  return 0
}

ctfvm_provider_digitalocean_delete_instance() {
  local instance="$1"
  ctfvm_doctl compute droplet delete "${instance}" --force
}

ctfvm_provider_digitalocean_registry_image_ref() {
  local _project="$1"
  local _location="$2"
  local repository="$3"
  local image_name="$4"
  local tag="$5"
  printf '%s\n' "registry.digitalocean.com/${repository}/${image_name}:${tag}"
}

ctfvm_provider_digitalocean_registry_prepare_local_push() {
  local _project="$1"
  local location="$2"
  local repository="$3"
  local registry_tier="${4:-basic}"

  if ! ctfvm_doctl registry get "${repository}" >/dev/null 2>&1; then
    ctfvm_doctl registry create "${repository}" --subscription-tier "${registry_tier}" --region "${location}" >/dev/null
  fi
  ctfvm_doctl registry login "${repository}" --expiry-seconds 3600 >/dev/null
}

ctfvm_provider_digitalocean_registry_pull_remote() {
  local image_ref="$1"
  local registry_path="${image_ref#registry.digitalocean.com/}"
  local registry_name="${registry_path%%/*}"
  local docker_config docker_config_b64 remote_cmd remote_cmd_q

  if [[ -z "${registry_name}" || "${registry_name}" == "${image_ref}" ]]; then
    remote_cmd="set -euo pipefail; docker pull ${image_ref}; docker tag ${image_ref} ctf-toolbox:latest"
    printf -v remote_cmd_q '%q' "${remote_cmd}"
    gcloud_ssh "sudo -u ctf bash -lc ${remote_cmd_q}"
    return 0
  fi

  docker_config="$(ctfvm_doctl registry docker-config "${registry_name}" --expiry-seconds 3600)"
  docker_config_b64="$(printf '%s' "${docker_config}" | base64 | tr -d '\n')"
  remote_cmd="set -euo pipefail; mkdir -p /home/ctf/.docker; printf '%s' '${docker_config_b64}' | base64 -d > /home/ctf/.docker/config.json; chmod 600 /home/ctf/.docker/config.json; docker pull ${image_ref}; docker tag ${image_ref} ctf-toolbox:latest"
  printf -v remote_cmd_q '%q' "${remote_cmd}"
  gcloud_ssh "sudo -u ctf bash -lc ${remote_cmd_q}"
}

ctfvm_provider_digitalocean_prepare_vscode_host() {
  local instance="$1"
  local _zone="$2"
  local _project="$3"
  local ip="$4"
  local host_alias known_hosts config_dir config_file tmp_file
  if [[ -z "${ip}" ]]; then
    return 0
  fi
  host_alias="$(ctfvm_provider_digitalocean_ssh_alias "${instance}" "${ip}")"
  known_hosts="$(ctfvm_provider_digitalocean_known_hosts_file)"
  config_dir="${HOME}/.ssh"
  config_file="${config_dir}/config"
  mkdir -p "${config_dir}"
  touch "${config_file}"
  chmod 600 "${config_file}" 2>/dev/null || true
  tmp_file="$(mktemp)"
  awk -v begin="# >>> ctfvm ${host_alias} >>>" -v end="# <<< ctfvm ${host_alias} <<<" '
    $0 == begin { skip = 1; next }
    $0 == end { skip = 0; next }
    !skip { print }
  ' "${config_file}" > "${tmp_file}" || true
  cat >> "${tmp_file}" <<EOF

# >>> ctfvm ${host_alias} >>>
Host ${host_alias}
  HostName ${ip}
  User root
  StrictHostKeyChecking accept-new
  UserKnownHostsFile ${known_hosts}
  GlobalKnownHostsFile /dev/null
  HostKeyAlias ${host_alias}
# <<< ctfvm ${host_alias} <<<
EOF
  mv "${tmp_file}" "${config_file}"
}

ctfvm_provider_digitalocean_vscode_host_alias() {
  local instance="$1"
  local _zone="$2"
  local _project="$3"
  local ip="$4"
  printf '%s\n' "$(ctfvm_provider_digitalocean_ssh_alias "${instance}" "${ip}")"
}
