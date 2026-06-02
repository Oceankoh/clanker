"""Local secret storage for agent credentials that can't be derived elsewhere.

Backs `clanker auth claude`: the Claude subscription OAuth token from
`claude setup-token` is stored here (it is not a file we can copy from the
operator's machine — see docs/AGENTS.md §2). Stored at ``.ctfvm/secrets.json``
with ``0600`` perms; ``.ctfvm/`` is gitignored.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import STATE_DIR, load_json

SECRETS_PATH = STATE_DIR / "secrets.json"


def load_secrets(path: Path = SECRETS_PATH) -> dict:
    data = load_json(path)
    return data if isinstance(data, dict) else {}


def _write_secrets(data: dict, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def set_secret(key: str, value: str, *, path: Path = SECRETS_PATH) -> None:
    data = load_secrets(path)
    data[key] = value
    _write_secrets(data, path)


def get_secret(key: str, *, path: Path = SECRETS_PATH) -> str:
    return str(load_secrets(path).get(key, "") or "")


# --- credential profiles (multi-subscription) ------------------------------
# secrets.json["profiles"][name] = {"backend": "...", "<settings-key>": "...", ...}
# A profile's keys are Settings keys (claude_oauth_token / openai_api_key /
# codex_home / no_auth_sync / model / …) and overlay Settings at high priority.

def set_profile(name: str, data: dict, *, path: Path = SECRETS_PATH) -> None:
    secrets = load_secrets(path)
    profiles = secrets.setdefault("profiles", {})
    existing = profiles.get(name) or {}
    existing.update({k: v for k, v in data.items() if v not in (None, "")})
    profiles[name] = existing
    _write_secrets(secrets, path)


def get_profile(name: str, *, path: Path = SECRETS_PATH) -> dict:
    profiles = load_secrets(path).get("profiles") or {}
    return dict(profiles.get(name) or {})


def list_profiles(*, path: Path = SECRETS_PATH) -> dict:
    return dict(load_secrets(path).get("profiles") or {})
