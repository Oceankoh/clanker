"""Small subprocess helper — the one place we shell out to cloud CLIs.

Mirrors the legacy ``ctfvm_ui.common.run_cmd`` contract: returns
``(returncode, stdout, stderr)`` and maps a timeout to rc 124 instead of
raising.
"""

from __future__ import annotations

import subprocess

from .config import ROOT


def run_cmd(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
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
        # cloud CLI not installed
        return 127, "", str(exc)
