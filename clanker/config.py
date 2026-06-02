"""Paths, constants, and the settings-precedence resolver.

Precedence (highest first), per CTFVM.md:
    CLI flag  >  repo .env  >  cloud-CLI environment (os.environ)
              >  .ctfvm/config.json  >  built-in default
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


# ---------------------------------------------------------------------------
# Settings schema — one registry of every knob the Python core consumes, with a
# consistent env binding so `.env` can drive everything (see .env.example).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigKey:
    name: str          # the Settings key
    env_var: str       # the .env / environment variable that sets it
    default: str = ""
    group: str = "general"
    help: str = ""
    secret: bool = False
    consumed_by: str = "python"  # "python" | "bash" | "both" (for docs / `config show`)


SETTINGS_SCHEMA: list[ConfigKey] = [
    # --- agent backend ---
    ConfigKey("agent_backend", "CTFVM_AGENT", "codex", "agent", "Default agent backend (codex|claude-code)", consumed_by="both"),
    ConfigKey("model", "CTFVM_MODEL", "", "agent", "Default agent model (blank = backend default)", consumed_by="both"),
    ConfigKey("codex_home", "CTFVM_CODEX_HOME", str(Path.home() / ".codex"), "agent", "Local Codex session dir to sync"),
    ConfigKey("no_auth_sync", "CTFVM_NO_AUTH_SYNC", "", "agent", "Skip Codex session sync (use API key instead)"),
    ConfigKey("openai_api_key", "OPENAI_API_KEY", "", "agent", "Codex API key (alternative to session sync)", secret=True),
    ConfigKey("anthropic_api_key", "ANTHROPIC_API_KEY", "", "agent", "Claude API key (alternative to OAuth token)", secret=True),
    ConfigKey("claude_oauth_token", "CLAUDE_CODE_OAUTH_TOKEN", "", "agent", "Claude OAuth token (prefer `clanker auth claude`)", secret=True),
    ConfigKey("ida_mcp_url", "CTFVM_DEFAULT_IDA_MCP_URL", "", "agent", "Remote IDA MCP server URL"),
    # --- cloud (consumed by the bash CLI; shown here for one-stop config) ---
    ConfigKey("provider", "CTFVM_PROVIDER", "gcp", "cloud", "Default cloud provider (gcp|digitalocean)", consumed_by="bash"),
    ConfigKey("gcp_project", "CTFVM_GCP_PROJECT", "", "cloud", "GCP project", consumed_by="bash"),
    ConfigKey("gcp_zone", "CTFVM_GCP_ZONE", "", "cloud", "GCP zone", consumed_by="bash"),
    ConfigKey("gcp_machine_type", "CTFVM_GCP_MACHINE_TYPE", "e2-standard-4", "cloud", "GCP machine type", consumed_by="bash"),
    ConfigKey("do_region", "CTFVM_DO_REGION", "", "cloud", "DigitalOcean region", consumed_by="bash"),
    ConfigKey("do_size_slug", "CTFVM_DO_SIZE_SLUG", "s-4vcpu-8gb", "cloud", "DigitalOcean droplet size", consumed_by="bash"),
    # --- control plane / run ---
    ConfigKey("control_port", "CTFVM_CONTROL_PORT", "443", "control", "Control-plane port", consumed_by="both"),
    ConfigKey("timeout_min", "CTFVM_TIMEOUT_MIN", "1440", "control", "VM self-destruct timeout (minutes)", consumed_by="both"),
    ConfigKey("toolbox_variant", "CTFVM_TOOLBOX_VARIANT", "lean", "control", "Toolbox image variant (lean|full)", consumed_by="both"),
    ConfigKey("ui_token", "CTFVM_UI_TOKEN", "", "control", "UI auth token (set/non-empty -> server requires it)", secret=True),
    # --- vpn ---
    ConfigKey("vpn", "CTFVM_VPN", "1", "vpn", "Auto-start WireGuard VPN (1|0)", consumed_by="bash"),
    ConfigKey("vpn_cidrs", "CTFVM_VPN_CIDRS", "", "vpn", "Comma-separated local CIDRs to route", consumed_by="bash"),
]

_SCHEMA_BY_NAME = {k.name: k for k in SETTINGS_SCHEMA}


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

    def __init__(
        self,
        root: Path = ROOT,
        cli: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
    ):
        self.root = Path(root)
        self._cli = {k: v for k, v in (cli or {}).items() if v is not None}
        # a selected credential profile overlays everything except explicit CLI flags
        self._profile = {k: v for k, v in (profile or {}).items() if v not in (None, "")}
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
        return self.resolve(key, env_var=env_var, config_key=config_key, default=default)[0]

    def resolve(
        self,
        key: str,
        *,
        env_var: str | None = None,
        config_key: str | None = None,
        default: Any = None,
    ) -> tuple[Any, str]:
        """Return (value, source) where source is one of cli/.env/env/secret/
        config.json/default. Unspecified ``env_var``/``default`` fall back to the
        schema registration for ``key`` so every known knob is env-addressable."""
        spec = _SCHEMA_BY_NAME.get(key)
        if env_var is None and spec:
            env_var = spec.env_var
        if default is None and spec:
            default = spec.default

        if key in self._cli:
            return self._cli[key], "cli"
        if key in self._profile:
            return self._profile[key], "profile"
        if env_var:
            if env_var in self._dotenv:
                return self._dotenv[env_var], ".env"
            if env_var in os.environ:
                return os.environ[env_var], "env"
        if key in self._secrets:
            return self._secrets[key], "secret"
        ck = config_key or key
        if ck in self._config_json:
            return self._config_json[ck], ".ctfvm/config.json"
        return default, "default"

    def effective(self) -> list[tuple[ConfigKey, Any, str]]:
        """Every schema key resolved, with provenance — powers `clanker config show`."""
        out: list[tuple[ConfigKey, Any, str]] = []
        for spec in SETTINGS_SCHEMA:
            value, source = self.resolve(spec.name)
            out.append((spec, value, source))
        return out
