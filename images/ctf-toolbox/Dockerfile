# syntax=docker/dockerfile:1.7
FROM debian:12

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ARG CTF_TOOLBOX_VARIANT=lean

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
  apt-get update && apt-get install -y --no-install-recommends \
  binutils \
  build-essential \
  ca-certificates \
  curl \
  dnsutils \
  file \
  gdb \
  git \
  iproute2 \
  iputils-ping \
  jq \
  less \
  libc6-dbg \
  libssl-dev \
  netcat-openbsd \
  nodejs \
  npm \
  python3 \
  python3-dev \
  python3-venv \
  socat \
  sudo \
  strace \
  tcpdump \
  unzip \
  vim \
  wget \
  && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

RUN npm config set fund false \
  && npm config set update-notifier false \
  && (npm install -g @openai/codex || npm install -g codex-cli)

RUN if [ "${CTF_TOOLBOX_VARIANT}" = "full" ]; then \
    apt-get update && apt-get install -y --no-install-recommends \
      sagemath \
      && (apt-get install -y --no-install-recommends radare2 || true) \
      && rm -rf /var/lib/apt/lists/*; \
  fi

COPY install-python-runtime.sh /usr/local/bin/install-python-runtime.sh
RUN chmod +x /usr/local/bin/install-python-runtime.sh

COPY addons /opt/ctf-toolbox/addons
RUN chmod +x /opt/ctf-toolbox/addons/*.sh

COPY codex-config /opt/ctf-toolbox/codex-config

RUN useradd -m -s /bin/bash ctf \
  && usermod -aG sudo ctf \
  && echo 'ctf ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/ctf \
  && chmod 0440 /etc/sudoers.d/ctf
USER ctf
WORKDIR /workspace
ENV HOME=/home/ctf
ENV VIRTUAL_ENV=/home/ctf/.venv
ENV PATH=/home/ctf/.venv/bin:/usr/local/bin:$PATH
ENV CTF_ADDONS_DIR=/opt/ctf-toolbox/addons

CMD ["sleep", "infinity"]
