#!/usr/bin/env bash
set -euo pipefail

exec > >(tee -a /var/log/ctfvm-startup.log) 2>&1

export DEBIAN_FRONTEND=noninteractive
CTFVM_PROVIDER="__CTFVM_PROVIDER__"
TIMEOUT_MIN="__CTFVM_TIMEOUT_MIN__"

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3
fi

if ! id -u ctf >/dev/null 2>&1; then
  useradd -m -s /bin/bash ctf
fi

mkdir -p /home/ctf/run /opt/licensed
chown -R ctf:ctf /home/ctf/run /opt/licensed

__CTFVM_CONTROL_SERVER_INSTALL__

chmod +x /usr/local/bin/ctfvm-control-server.py

cat > /etc/systemd/system/ctfvm-control.service <<UNIT
[Unit]
Description=CTFVM HTTP control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Restart=always
RestartSec=2
ExecStart=/usr/bin/env python3 /usr/local/bin/ctfvm-control-server.py --host 0.0.0.0 --port __CTFVM_CONTROL_PORT__ --user __CTFVM_CONTROL_USER__ --password __CTFVM_CONTROL_PASSWORD__ --provider __CTFVM_PROVIDER__

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now ctfvm-control.service

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  nodejs \
  npm \
  jq \
  python3 \
  socat \
  tmux \
  wireguard-tools \
  docker.io

install_codex_prefix() {
  local prefix="/opt/ctfvm/npm-global"
  mkdir -p "${prefix}"
  chmod 755 /opt /opt/ctfvm "${prefix}"

  npm config set fund false --global >/dev/null 2>&1 || true
  npm config set update-notifier false --global >/dev/null 2>&1 || true

  rm -f "${prefix}/bin/codex"
  rm -rf "${prefix}/lib/node_modules/@openai/codex" "${prefix}/lib/node_modules/codex-cli"

  if npm install -g --prefix "${prefix}" @openai/codex; then
    echo "Installed Codex into ${prefix} from @openai/codex."
  else
    echo "Falling back to codex-cli package install."
    npm install -g --prefix "${prefix}" codex-cli
  fi

  if [[ ! -x "${prefix}/bin/codex" ]]; then
    echo "Codex install did not produce ${prefix}/bin/codex." >&2
    exit 1
  fi
}

install_claude_prefix() {
  local prefix="/opt/ctfvm/npm-global"
  mkdir -p "${prefix}"
  chmod 755 /opt /opt/ctfvm "${prefix}"

  rm -f "${prefix}/bin/claude"
  rm -rf "${prefix}/lib/node_modules/@anthropic-ai/claude-code"

  if npm install -g --prefix "${prefix}" @anthropic-ai/claude-code; then
    echo "Installed Claude Code into ${prefix} from @anthropic-ai/claude-code."
  else
    echo "Claude Code install failed." >&2
    exit 1
  fi

  if [[ ! -x "${prefix}/bin/claude" ]]; then
    echo "Claude Code install did not produce ${prefix}/bin/claude." >&2
    exit 1
  fi
}

install_codex_prefix
install_claude_prefix

systemctl enable --now docker
usermod -aG docker ctf

cat > /usr/local/bin/ctfvm-self-destruct.sh <<SCRIPT
#!/usr/bin/env bash
set -euo pipefail

PROVIDER="${CTFVM_PROVIDER}"

if [[ "\${PROVIDER}" == "gcp" ]]; then
  md() {
    curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/\$1"
  }

  PROJECT="\$(md project/project-id)"
  ZONE_FULL="\$(md instance/zone)"
  ZONE="\${ZONE_FULL##*/}"
  INSTANCE="\$(md instance/name)"

  TOKEN="\$(curl -fsS -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r '.access_token')"

  if [[ -z "\${TOKEN}" || "\${TOKEN}" == "null" ]]; then
    shutdown -h now
    exit 0
  fi

  API="https://compute.googleapis.com/compute/v1/projects/\${PROJECT}/zones/\${ZONE}/instances/\${INSTANCE}"
  HTTP_CODE="\$(curl -s -o /tmp/ctfvm-delete-resp.json -w '%{http_code}' -X DELETE \
    -H "Authorization: Bearer \${TOKEN}" \
    -H "Content-Type: application/json" \
    "\${API}")"

  if [[ "\${HTTP_CODE}" -lt 200 || "\${HTTP_CODE}" -ge 300 ]]; then
    shutdown -h now
  fi
else
  shutdown -h now
fi
SCRIPT

chmod +x /usr/local/bin/ctfvm-self-destruct.sh

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

mkdir -p /var/lib/ctfvm
touch /var/lib/ctfvm/bootstrap-ready

echo "Startup complete. Timeout set to ${TIMEOUT_MIN} minutes."
