#!/usr/bin/env bash
set -euo pipefail

exec > >(tee -a /var/log/ctfvm-startup.log) 2>&1

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  jq \
  tmux \
  docker.io

systemctl enable --now docker

if ! id -u ctf >/dev/null 2>&1; then
  useradd -m -s /bin/bash ctf
fi
usermod -aG docker ctf

mkdir -p /home/ctf/run /opt/licensed
chown -R ctf:ctf /home/ctf/run /opt/licensed

cat > /usr/local/bin/ctfvm-self-destruct.sh <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail

md() {
  curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"
}

PROJECT="$(md project/project-id)"
ZONE_FULL="$(md instance/zone)"
ZONE="${ZONE_FULL##*/}"
INSTANCE="$(md instance/name)"

TOKEN="$(curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r '.access_token')"

if [[ -z "${TOKEN}" || "${TOKEN}" == "null" ]]; then
  shutdown -h now
  exit 0
fi

API="https://compute.googleapis.com/compute/v1/projects/${PROJECT}/zones/${ZONE}/instances/${INSTANCE}"
HTTP_CODE="$(curl -s -o /tmp/ctfvm-delete-resp.json -w '%{http_code}' -X DELETE \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${API}")"

if [[ "${HTTP_CODE}" -lt 200 || "${HTTP_CODE}" -ge 300 ]]; then
  shutdown -h now
fi
SCRIPT

chmod +x /usr/local/bin/ctfvm-self-destruct.sh

TIMEOUT_MIN="$(curl -fsS -H 'Metadata-Flavor: Google' \
  'http://metadata.google.internal/computeMetadata/v1/instance/attributes/ctfvm-timeout-min' || echo 1440)"

cat > /etc/systemd/system/ctfvm-self-destruct.service <<UNIT
[Unit]
Description=Self-destruct CTF VM after timeout
After=network-online.target docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/ctfvm-self-destruct.sh
UNIT

cat > /etc/systemd/system/ctfvm-self-destruct.timer <<UNIT
[Unit]
Description=Timer for CTF VM self-destruct

[Timer]
OnBootSec=${TIMEOUT_MIN}min
Unit=ctfvm-self-destruct.service
Persistent=false

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now ctfvm-self-destruct.timer

echo "Startup complete. Timeout set to ${TIMEOUT_MIN} minutes."
