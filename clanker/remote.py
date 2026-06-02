"""Helpers for running code on the VM via the control plane.

Rather than build fragile bash with embedded markers (BUGS.md B1), the
snapshot/artifact paths ship a small Python program to the VM and run it as the
``ctf`` user. The program is base64-encoded so it needs no shell quoting, and it
emits a single JSON object on stdout.
"""

from __future__ import annotations

import base64


def remote_python(py_source: str, *, user: str = "ctf") -> str:
    """Wrap a Python program as a control-plane shell command.

    base64 uses only ``[A-Za-z0-9+/=]`` — safe to embed unquoted — so the inner
    program can contain any quotes/newlines without escaping concerns.
    """
    b64 = base64.b64encode(py_source.encode("utf-8")).decode("ascii")
    return f"printf %s {b64} | base64 -d | sudo -u {user} python3 -"
