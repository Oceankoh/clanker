"""Input validation for the monitoring/steering/artifact paths.

Ported verbatim from the current code (these checks are already correct — see
BUGS.md R1/R2). The refactor PRESERVES them; it does not introduce them. All
validation runs locally, before any remote call (Invariant 5).
"""

from __future__ import annotations

import re

# tmux target: <session>:<window>, allowlisted charset (R2).
TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
# special-key names accepted by `tmux send-keys` (C-c, Enter, Escape, …).
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def safe_target(target: str | None) -> str | None:
    """Validate a tmux target; default to ctf:supervisor; reject anything else."""
    target = (target or "ctf:supervisor").strip() or "ctf:supervisor"
    if not TARGET_RE.match(target):
        return None
    return target


def target_session(target: str) -> str:
    if ":" not in target:
        return "ctf"
    return target.split(":", 1)[0]


def safe_keys(keys: list[str] | None) -> list[str]:
    return [k for k in (keys or []) if KEY_RE.match(k or "")]


def sanitize_relpath(relpath: str | None) -> str | None:
    """Return a clean artifact-relative path or None (R1).

    Rules: no absolute / ``~`` paths; reject any ``..`` component; strip ``.``;
    must live under ``artifacts/``.
    """
    relpath = (relpath or "").strip().replace("\\", "/")
    if not relpath:
        return None
    if relpath.startswith("/") or relpath.startswith("~"):
        return None
    parts = [part for part in relpath.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return None
    clean = "/".join(parts)
    if not clean.startswith("artifacts/"):
        return None
    return clean
