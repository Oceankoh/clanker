"""Tmux steering — control-plane only, base64 transport.

Validates targets/keys locally before any remote call (Invariant 5, R2) and
sends text base64-encoded so no shell quoting of user content is needed. Uses
the typed control client exclusively (B7); there is no SSH path here.
"""

from __future__ import annotations

import base64
import json
import shlex
from datetime import datetime, timezone

from .controlclient import ControlPlaneClient
from .validation import safe_keys, safe_target, target_session


class SteeringError(ValueError):
    """Invalid steering input (bad target/keys/state) — maps to 400."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_text(client: ControlPlaneClient, target: str, text: str, *, enter: bool = True, timeout: int = 20) -> None:
    tgt = safe_target(target)
    if not tgt:
        raise SteeringError("Invalid tmux target")
    if not text:
        raise SteeringError("text is required")
    session = target_session(tgt)
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    enter_cmd = f"sleep 0.08; tmux send-keys -t {shlex.quote(tgt)} C-m;" if enter else ""
    remote = (
        "sudo -u ctf bash -lc '"
        f"tmux has-session -t {shlex.quote(session)} >/dev/null 2>&1 || exit 1; "
        f'payload="$(printf %s {shlex.quote(payload)} | base64 -d)"; '
        f'tmux send-keys -t {shlex.quote(tgt)} -l -- "${{payload}}"; '
        f"{enter_cmd}"
        "'"
    )
    _exec_ok(client, remote, timeout, "send failed")


def send_keys(client: ControlPlaneClient, target: str, keys: list[str], *, timeout: int = 15) -> None:
    tgt = safe_target(target)
    if not tgt:
        raise SteeringError("Invalid tmux target")
    valid = safe_keys(keys)
    if not valid:
        raise SteeringError("No valid keys provided")
    session = target_session(tgt)
    keys_cmd = " ".join(shlex.quote(k) for k in valid)
    remote = (
        "sudo -u ctf bash -lc '"
        f"tmux has-session -t {shlex.quote(session)} >/dev/null 2>&1 || exit 1; "
        f"tmux send-keys -t {shlex.quote(tgt)} {keys_cmd}'"
    )
    _exec_ok(client, remote, timeout, "key failed")


def trust_prompt(client: ControlPlaneClient, target: str = "ctf:supervisor") -> None:
    send_keys(client, target, ["1", "Enter"])


def inject_message(client: ControlPlaneClient, text: str, *, timeout: int = 15) -> None:
    """Queue a message for the agent (the safe path): append to the run's
    inject.queue, which the tmux bridge feeds to the agent at a natural point —
    as opposed to `send_text`, which types into the live pane immediately."""
    if not text or not text.strip():
        raise SteeringError("text is required")
    payload = base64.b64encode((text.rstrip("\n") + "\n").encode("utf-8")).decode("ascii")
    remote = (
        "sudo -u ctf bash -lc '"
        f"printf %s {shlex.quote(payload)} | base64 -d >> /home/ctf/run/inject.queue"
        "'"
    )
    _exec_ok(client, remote, timeout, "inject failed")


def set_explicit_status(client: ControlPlaneClient, state: str, note: str = "", *, timeout: int = 15) -> None:
    state = str(state or "").strip().lower()
    if state not in {"solved", "blocked"}:
        raise SteeringError("Invalid explicit status")
    payload = {"state": state, "updated_at": _utc_now_iso()}
    note = str(note or "").strip()
    if note:
        payload["note"] = note
    encoded = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    remote = (
        "sudo -u ctf bash -lc '"
        f"printf %s {shlex.quote(encoded)} | base64 -d > /home/ctf/run/ui-status.json"
        "'"
    )
    _exec_ok(client, remote, timeout, "status update failed")


def clear_explicit_status(client: ControlPlaneClient, *, timeout: int = 15) -> None:
    remote = "sudo -u ctf bash -lc 'rm -f /home/ctf/run/ui-status.json'"
    _exec_ok(client, remote, timeout, "status clear failed")


def _exec_ok(client: ControlPlaneClient, remote: str, timeout: int, what: str) -> None:
    from .controlclient import ControlPlaneError

    result = client.exec(remote, timeout=timeout)
    if not result.ok:
        raise ControlPlaneError(result.stderr_text().strip() or what)
