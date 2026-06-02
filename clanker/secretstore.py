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


def set_secret(key: str, value: str, *, path: Path = SECRETS_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_secrets(path)
    data[key] = value
    # write 0600 atomically-ish
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_secret(key: str, *, path: Path = SECRETS_PATH) -> str:
    return str(load_secrets(path).get(key, "") or "")
