#!/usr/bin/env bash
# install-toolbox.sh — provision a VM host with the clanker toolbox, directly on
# the host (no Docker). This is the single source of truth for what the golden
# image contains, and mirrors images/ctf-toolbox/Dockerfile. Run as root on a
# fresh builder VM during `clanker image bake`.
#
# IMPORTANT: this installs TOOLS and AGENT CLIs only. It MUST NOT write any
# credentials (OAuth tokens, API keys, auth.json). Credentials are injected
# per-challenge at agent launch — see docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md §1.5.
set -euo pipefail

VARIANT="${CTF_TOOLBOX_VARIANT:-lean}"
NPM_PREFIX="/opt/ctfvm/npm-global"
TOOLBOX_DIR="/opt/ctf-toolbox"
MARKER="/var/lib/ctfvm/golden-ready"
VERSIONS_FILE="/var/lib/ctfvm/golden-versions.env"

log() { echo "[install-toolbox] $*"; }
die() { echo "[install-toolbox] ERROR: $*" >&2; exit 1; }

retry() {
  local attempts="$1"
  shift
  local n=1
  until "$@"; do
    if [ "$n" -ge "$attempts" ]; then
      return 1
    fi
    sleep $((n * 2))
    n=$((n + 1))
  done
}

download_file() {
  local url="$1"
  local dest="$2"
  if [ -f "$dest" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$dest")"
  local tmp
  tmp="${dest}.tmp.$$"
  rm -f "$tmp"
  retry 3 curl -fsSL "$url" -o "$tmp"
  mv "$tmp" "$dest"
}

append_bashrc_line() {
  local bashrc="$1"
  local line="$2"
  touch "$bashrc"
  if ! grep -qxF "$line" "$bashrc"; then
    printf '%s\n' "$line" >> "$bashrc"
  fi
}

# Fully non-interactive apt: DEBIAN_FRONTEND stops debconf prompts, and the
# NEEDRESTART_* vars stop Ubuntu 24.04's needrestart from prompting "which
# services to restart?" (that prompt over a non-TTY SSH hangs the whole bake).
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1

log "apt packages (mirrors the toolbox Dockerfile)…"
apt-get update
apt-get install -y --no-install-recommends \
  binutils build-essential ca-certificates curl dnsutils file gdb git ripgrep \
  iproute2 iputils-ping jq less libc6-dbg libssl-dev netcat-openbsd nodejs npm \
  python3 python3-dev python3-pip python3-venv socat sudo strace tcpdump tmux unzip vim wget

log "uv…"
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

if [ "${VARIANT}" = "full" ]; then
  log "full variant: sagemath + radare2 (best-effort)…"
  apt-get install -y --no-install-recommends sagemath || true
  apt-get install -y --no-install-recommends radare2 || true
fi

# gdb attach across same-user processes on the host (ptrace_scope=0). Baked into
# the image via sysctl drop-in so it survives reboots.
log "ptrace_scope=0 (gdb attach)…"
echo 'kernel.yama.ptrace_scope = 0' > /etc/sysctl.d/10-ctfvm-ptrace.conf

log "ctf user + passwordless sudo…"
id ctf >/dev/null 2>&1 || useradd -m -s /bin/bash ctf
usermod -aG sudo ctf
echo 'ctf ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ctf
chmod 0440 /etc/sudoers.d/ctf

log "agent CLIs into ${NPM_PREFIX} (no credentials)…"
mkdir -p "${NPM_PREFIX}"
npm install -g --prefix "${NPM_PREFIX}" @openai/codex || npm install -g --prefix "${NPM_PREFIX}" codex-cli || true
npm install -g --prefix "${NPM_PREFIX}" @anthropic-ai/claude-code || true
chown -R root:root "${NPM_PREFIX}"

# Stage the MCP server + config templates next to where the runner expects them.
# bake.sh ships these from images/ctf-toolbox/{mcp,codex-config}.
if [ -d /tmp/ctf-toolbox-payload ]; then
  log "staging mcp + config templates…"
  mkdir -p "${TOOLBOX_DIR}"
  cp -r /tmp/ctf-toolbox-payload/mcp "${TOOLBOX_DIR}/mcp" 2>/dev/null || true
  cp -r /tmp/ctf-toolbox-payload/codex-config "${TOOLBOX_DIR}/codex-config" 2>/dev/null || true
fi

log "IDA Pro 9.3…"
IDA_DIR="/opt/ida-pro-9.3"
IDA_INSTALLER_URL="${CTFVM_IDA_INSTALLER_URL:-}"
IDA_INSTALLER_PATH="${CTFVM_IDA_INSTALLER_PATH:-}"
if [ ! -x "$IDA_DIR/idat" ]; then
  if [ -n "$IDA_INSTALLER_PATH" ]; then
    [ -f "$IDA_INSTALLER_PATH" ] || die "CTFVM_IDA_INSTALLER_PATH does not exist: ${IDA_INSTALLER_PATH}"
    cp "$IDA_INSTALLER_PATH" /tmp/ida-pro-93.run
  elif [ -n "$IDA_INSTALLER_URL" ]; then
    download_file "$IDA_INSTALLER_URL" /tmp/ida-pro-93.run
  else
    die "IDA is not installed; set CTFVM_IDA_INSTALLER_URL or CTFVM_IDA_INSTALLER_PATH for the licensed IDA Pro 9.3 installer"
  fi
  chmod +x /tmp/ida-pro-93.run
  /tmp/ida-pro-93.run --mode unattended --prefix "$IDA_DIR"
  rm -f /tmp/ida-pro-93.run

else
  log "IDA Pro already installed at $IDA_DIR"
fi

if [ -d "$IDA_DIR/idalib/python" ]; then
  pushd "$IDA_DIR/idalib/python" >/dev/null
  if ! python3 -c 'import idapro' >/dev/null 2>&1 && compgen -G "idapro*.whl" >/dev/null; then
    # Keep pip here: this is a vendor-provided local IDA wheel.
    python3 -m pip install idapro*.whl --break-system-packages --force-reinstall
  fi
  python3 ./py-activate-idalib.py
  popd >/dev/null
fi

python3 -c "
import idapro
import ida_registry
for i in range(10):
    ida_registry.reg_write_int(f'EULA 9{i}', 1)
"

append_bashrc_line /root/.bashrc 'export PATH=$PATH:/opt/ida-pro-9.3'
append_bashrc_line /home/ctf/.bashrc 'export PATH=$PATH:/opt/ida-pro-9.3'
chown ctf:ctf /home/ctf/.bashrc

"$IDA_DIR/idat" -A -B -o/tmp/ida_test.i64 /usr/bin/true
test -f /tmp/ida_test.i64
rm -f /tmp/ida_test.i64
log "IDA Pro 9.3 installed and verified"

log "pre-warm base venv for the ctf user…"
sudo -u ctf bash -lc 'python3 -m venv /home/ctf/.venv && /home/ctf/.venv/bin/pip install --upgrade pip pwntools z3-solver' || true

# Record agent versions for `clanker image status` (consumed by bake.sh).
CODEX_VER="$(${NPM_PREFIX}/bin/codex --version 2>/dev/null | head -n1 | tr -d '\r' || true)"
CLAUDE_VER="$(${NPM_PREFIX}/bin/claude --version 2>/dev/null | head -n1 | tr -d '\r' || true)"
mkdir -p "$(dirname "${VERSIONS_FILE}")"
{
  echo "CODEX_VERSION=${CODEX_VER}"
  echo "CLAUDE_VERSION=${CLAUDE_VER}"
} > "${VERSIONS_FILE}"

# Belt-and-suspenders: never let a credential leak into the image.
rm -f /home/ctf/.codex/auth.json /home/ctf/.claude/.credentials.json 2>/dev/null || true

rm -rf /var/lib/apt/lists/*
mkdir -p "$(dirname "${MARKER}")"
touch "${MARKER}"
log "done — golden image ready (marker: ${MARKER})"
