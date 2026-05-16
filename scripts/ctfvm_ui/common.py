#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".ctfvm"
STATE_FILE = ROOT / ".ctfvm" / "current-run.json"
RUNS_DIR = ROOT / ".ctfvm" / "runs"
MAX_ARTIFACT_PREVIEW_BYTES = 2_000_000
MAX_PANE_TAIL_BYTES = 30_000
MAX_FINDINGS_TAIL_BYTES = 24_000
MAX_INJECT_TAIL_BYTES = 12_000
DISCOVERY_TTL_SECONDS = 15


def run_cmd(cmd, timeout=20):
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"
    except FileNotFoundError as exc:
        return 127, "", f"{exc.filename or (cmd[0] if cmd else 'command')}: not found"


def load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def state_valid(state):
    if not isinstance(state, dict):
        return False
    provider = str((state or {}).get("provider", "") or "").strip().lower()
    if provider in {"do", "digitalocean", "digital-ocean"}:
        return bool(state.get("instance") and state.get("project"))
    return bool(state.get("instance") and state.get("zone") and state.get("project"))
