#!/usr/bin/env bash

ctfvm_provider_gcp_require_start_prereqs() {
  require_cmd gcloud
}

ctfvm_provider_gcp_require_registry_prereqs() {
  require_cmd gcloud
}

ctfvm_provider_gcp_default_location() {
  zone_from_gcloud_config
}

ctfvm_provider_gcp_default_scope() {
  project_from_gcloud_config
}

ctfvm_provider_gcp_default_registry_location() {
  local zone="${1:-}"
  region_from_zone "${zone}"
}

ctfvm_provider_gcp_list_running() {
  if ! command -v gcloud >/dev/null 2>&1; then
    return 0
  fi
  local project rows line name zone status ip
  project="$(project_from_gcloud_config)"
  if [[ -z "${project}" ]]; then
    return 0
  fi
  rows="$(gcloud compute instances list --project "${project}" --filter='name~^ctfvm- AND status=RUNNING' --format='value(name,zone,status,networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true)"
  while IFS= read -r line; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    name="$(awk '{print $1}' <<< "${line}")"
    zone="$(awk '{print $2}' <<< "${line}")"
    status="$(awk '{print $3}' <<< "${line}")"
    ip="$(awk '{print $4}' <<< "${line}")"
    zone="${zone##*/}"
    printf 'gcp\t%s\t%s\t%s\t%s\t%s\n' "${name}" "${zone}" "${project}" "${status}" "${ip}"
  done <<< "${rows}"
}

ctfvm_provider_gcp_get_status() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  gcloud compute instances describe "${instance}" --zone "${zone}" --project "${project}" --format='get(status)' 2>/dev/null || true
}

ctfvm_provider_gcp_get_ip() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  gcloud compute instances describe "${instance}" --zone "${zone}" --project "${project}" --format='get(networkInterfaces[0].accessConfigs[0].natIP)' 2>/dev/null || true
}

ctfvm_provider_gcp_ssh_exec() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  local _ip="$4"
  local cmd="$5"
  gcloud compute ssh "${instance}" --zone "${zone}" --project "${project}" --command "${cmd}"
}

ctfvm_provider_gcp_ssh_tty() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  local _ip="$4"
  local cmd="$5"
  gcloud compute ssh "${instance}" --zone "${zone}" --project "${project}" -- -t "${cmd}"
}

ctfvm_provider_gcp_ssh_argv() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  local _ip="$4"
  shift 4

  printf '%s\n' gcloud compute ssh "${instance}" --zone "${zone}" --project "${project}" --
  printf '%s\n' "$@"
}

ctfvm_provider_gcp_create_instance() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  local machine_type="$4"
  local startup_script="$5"
  local boot_image="$6"
  local boot_image_family="$7"
  local boot_image_project="$8"
  local network="${CTFVM_GCP_NETWORK:-}"
  local subnet="${CTFVM_GCP_SUBNET:-}"
  local tags="${CTFVM_GCP_NETWORK_TAGS:-ctfvm-control}"

  local -a create_args=(
    compute instances create "${instance}"
    --project "${project}"
    --zone "${zone}"
    --machine-type "${machine_type}"
    --boot-disk-size 80GB
    --scopes https://www.googleapis.com/auth/cloud-platform
    --metadata-from-file "startup-script=${startup_script}"
  )

  if [[ -n "${network}" ]]; then
    create_args+=(--network "${network}")
  fi
  if [[ -n "${subnet}" ]]; then
    create_args+=(--subnet "${subnet}")
  fi
  if [[ -n "${tags}" ]]; then
    create_args+=(--tags "${tags}")
  fi

  if [[ -n "${boot_image}" ]]; then
    create_args+=(--image "${boot_image}")
    if [[ -n "${boot_image_project}" ]]; then
      create_args+=(--image-project "${boot_image_project}")
    fi
  else
    create_args+=(--image-family "${boot_image_family}")
    if [[ -n "${boot_image_project}" ]]; then
      create_args+=(--image-project "${boot_image_project}")
    fi
  fi

  gcloud "${create_args[@]}"
}

ctfvm_provider_gcp_prepare_vpn_ingress() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  local port="$4"
  local network_url network rule tags

  network_url="$(gcloud compute instances describe "${instance}" --zone "${zone}" --project "${project}" --format='get(networkInterfaces[0].network)' 2>/dev/null || true)"
  network="${network_url##*/}"
  if [[ -z "${network}" ]]; then
    network="${CTFVM_GCP_NETWORK:-default}"
  fi

  tags="$(gcloud compute instances describe "${instance}" --zone "${zone}" --project "${project}" --format='value(tags.items)' 2>/dev/null | tr ';' ',' | tr '\t' ',' | sed -E 's/,+/,/g; s/^,+//; s/,+$//' || true)"
  rule="ctfvm-wireguard-udp-${port}"
  if gcloud compute firewall-rules describe "${rule}" --project "${project}" >/dev/null 2>&1; then
    return 0
  fi

  local -a args=(
    compute firewall-rules create "${rule}"
    --project "${project}"
    --network "${network}"
    --allow "udp:${port}"
    --source-ranges "0.0.0.0/0"
    --description "CTFVM WireGuard ingress for remote agents"
  )
  if [[ -n "${tags}" ]]; then
    args+=(--target-tags "${tags}")
  fi
  gcloud "${args[@]}" >/dev/null
}

ctfvm_provider_gcp_delete_instance() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  gcloud compute instances delete "${instance}" --zone "${zone}" --project "${project}" --quiet
}

ctfvm_provider_gcp_registry_image_ref() {
  local project="$1"
  local location="$2"
  local repository="$3"
  local image_name="$4"
  local tag="$5"
  printf '%s\n' "${location}-docker.pkg.dev/${project}/${repository}/${image_name}:${tag}"
}

ctfvm_provider_gcp_registry_prepare_local_push() {
  local project="$1"
  local location="$2"
  local repository="$3"

  gcloud services enable artifactregistry.googleapis.com --project "${project}" >/dev/null
  if ! gcloud artifacts repositories describe "${repository}" --location "${location}" --project "${project}" >/dev/null 2>&1; then
    gcloud artifacts repositories create "${repository}" \
      --repository-format docker \
      --location "${location}" \
      --project "${project}" \
      --description "ctfvm toolbox images"
  fi
  gcloud auth configure-docker "${location}-docker.pkg.dev" --quiet >/dev/null
}

ctfvm_provider_gcp_registry_pull_remote() {
  local image_ref="$1"
  local registry_host="${image_ref%%/*}"
  local remote_cmd remote_cmd_q

  if [[ "${registry_host}" == *-docker.pkg.dev ]]; then
    remote_cmd="set -euo pipefail; token=\"\$(curl -fsS -H 'Metadata-Flavor: Google' 'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' | jq -r '.access_token')\"; if [[ -z \"\${token}\" || \"\${token}\" == \"null\" ]]; then echo 'Failed to obtain access token from instance metadata.' >&2; exit 1; fi; printf '%s' \"\${token}\" | docker login -u oauth2accesstoken --password-stdin https://${registry_host}; docker pull ${image_ref}; docker tag ${image_ref} ctf-toolbox:latest"
  else
    remote_cmd="set -euo pipefail; docker pull ${image_ref}; docker tag ${image_ref} ctf-toolbox:latest"
  fi

  printf -v remote_cmd_q '%q' "${remote_cmd}"
  gcloud_ssh "sudo -u ctf bash -lc ${remote_cmd_q}"
}

ctfvm_provider_gcp_prepare_vscode_host() {
  local _instance="$1"
  local _zone="$2"
  local project="$3"
  gcloud compute config-ssh --project "${project}" --quiet >/dev/null
}

ctfvm_provider_gcp_vscode_host_alias() {
  local instance="$1"
  local zone="$2"
  local project="$3"
  printf '%s\n' "${instance}.${zone}.${project}"
}
