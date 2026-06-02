"""Paths, constants, and the settings-precedence resolver.

Precedence (highest first), per CTFVM.md:
    CLI flag  >  repo .env  >  cloud-CLI environment (os.environ)
              >  .ctfvm/config.json  >  built-in default
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Repo root: <root>/clanker/config.py -> parents[1] == <root>
ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".ctfvm"
STATE_FILE = STATE_DIR / "current-run.json"
RUNS_DIR = STATE_DIR / "runs"
CONFIG_JSON = STATE_DIR / "config.json"
ENV_FILE = ROOT / ".env"

# --- tunables (kept identical to the current code) --------------------------
STATUS_CACHE_TTL_SECONDS = 10
DISCOVERY_TTL_SECONDS = 15
DEFAULT_CONTROL_PORT = "443"
ACTIVE_RUNTIME_STATUSES = {"running", "active"}

MAX_ARTIFACT_PREVIEW_BYTES = 2_000_000
MAX_PANE_TAIL_BYTES = 30_000
MAX_FINDINGS_TAIL_BYTES = 24_000
MAX_SUPERVISOR_TAIL_BYTES = 16_000
RUN_BUNDLE_MAX_BYTES = 32_000_000
MAX_SPAWN_JOBS = 12
MAX_SPAWN_OUTPUT_BYTES = 64_000

SUPPORTED_AGENT_BACKENDS = ("codex", "claude-code")


def load_json(path: Path) -> Any:
    """Tolerant JSON load — returns ``None`` on any error (missing/malformed)."""
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def state_valid(state: Any) -> bool:
    """Port of ``common.state_valid`` — the validity gate used everywhere a
    ``.ctfvm`` json is read."""
    if not isinstance(state, dict):
        return False
    provider = str((state or {}).get("provider", "") or "").strip().lower()
    if provider in {"do", "digitalocean", "digital-ocean"}:
        return bool(state.get("instance") and state.get("project"))
    return bool(state.get("instance") and state.get("zone") and state.get("project"))


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` parser: ``KEY=value`` lines, ``#`` comments, optional
    surrounding quotes. No interpolation (matches the bash ``set -a`` source
    closely enough for resolution purposes)."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except Exception:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


class Settings:
    """Resolves configuration values across the precedence chain.

    ``cli`` is a mapping of already-parsed flag overrides (``None`` values are
    ignored). ``get`` looks up, in order: CLI override, ``.env`` entry,
    ``os.environ``, ``.ctfvm/config.json`` entry, then the supplied default.
    """

    def __init__(self, root: Path = ROOT, cli: dict[str, Any] | None = None):
        self.root = Path(root)
        self._cli = {k: v for k, v in (cli or {}).items() if v is not None}
        self._dotenv = _parse_env_file(self.root / ".env")
        self._config_json = load_json(self.root / ".ctfvm" / "config.json") or {}
        self._secrets = load_json(self.root / ".ctfvm" / "secrets.json") or {}

    def get(
        self,
        key: str,
        *,
        env_var: str | None = None,
        config_key: str | None = None,
        default: Any = None,
    ) -> Any:
        if key in self._cli:
            return self._cli[key]
        if env_var:
            if env_var in self._dotenv:
                return self._dotenv[env_var]
            if env_var in os.environ:
                return os.environ[env_var]
        # locally-stored secrets (e.g. Claude OAuth token) sit between env and config
        if key in self._secrets:
            return self._secrets[key]
        ck = config_key or key
        if ck in self._config_json:
            return self._config_json[ck]
        return default
