#!/usr/bin/env python3
"""Serve the clanker web UI with seeded fake data — for testing the frontend
without provisioning a VM.

    python3 scripts/preview-ui.py        # then open http://127.0.0.1:8800

Two fake runs (one codex, one claude-code), each with a realistic snapshot,
artifacts, and a backend-correct chat transcript. All endpoints are answered by
an in-process fake control client, so every button works end-to-end.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clanker.models import ExecResult  # noqa: E402
from clanker.server.app import App, make_handler  # noqa: E402
from clanker.server.service import UiService  # noqa: E402
from clanker.state import RunRegistry  # noqa: E402


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _snap(pane_text: str, findings: str, backend: str, status=None, extra_pane: str = "") -> dict:
    return {
        "error": None, "no_tmux": False,
        "panes": [
            {"session": "ctf", "index": "0", "name": "supervisor", "active": True,
             "target": "ctf:supervisor", "output_b64": _b64(pane_text + extra_pane)},
            {"session": "subagent-7f3c", "index": "0", "name": "exploit_tester", "active": False,
             "target": "subagent-7f3c:0", "output_b64": _b64("verifying ROP chain... confirmed")},
        ],
        "findings_b64": _b64(findings),
        "supervisor_b64": _b64(f"[supervisor] {backend} session active\n[bridge] subagent-7f3c mirrored"),
        "explicit_status": status,
        "metrics": {"now": 1000, "findings_mtime": 985, "supervisor_mtime": 995,
                    "artifact_mtime": 980, "artifact_count": 2, "inject_mtime": 0},
        "artifacts": [{"relpath": "artifacts/exploit.py", "size": 2048, "mtime": 980},
                      {"relpath": "artifacts/notes.md", "size": 512, "mtime": 970}],
    }


CODEX_ROLLOUT = "\n".join(json.dumps(o) for o in [
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "Solve the heap challenge in ./chal. Find the flag."}]}},
    {"type": "response_item", "payload": {"type": "reasoning",
        "summary": [{"type": "text", "text": "Use-after-free in the note editor. Leak libc, then ret2system."}]}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "I'll triage with checksec and find the UAF."}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "a",
        "arguments": json.dumps({"command": ["bash", "-lc", "checksec --file=./chal"]})}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "a",
        "output": "RELRO: Full\nStack: Canary\nNX: enabled\nPIE: enabled"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Double-free in free_note() gives a UAF; tcache-poison __free_hook."}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "apply_patch", "call_id": "b",
        "arguments": "*** write exploit.py (pwntools) ***"}},
    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "b", "output": "exploit.py written"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "Shell achieved. **Solved** - flag: `flag{clanker_demo}`."}]}},
])

CLAUDE_SESSION = "\n".join(json.dumps(o) for o in [
    {"type": "system", "subtype": "init"},
    {"type": "user", "message": {"role": "user", "content": "Reverse ./crackme and recover the flag."}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "Start by identifying the binary and any obvious string checks."},
        {"type": "text", "text": "I'll disassemble the check function and look for the comparison."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "objdump -d ./crackme | sed -n '/<check>:/,/ret/p'"}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x",
         "content": [{"type": "text", "text": "xor of input with 0x42, compared to a fixed buffer"}]}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "python3 -c \"print(bytes(b^0x42 for b in BUF))\""}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "y", "content": [{"type": "text", "text": "flag{x0r_is_not_crypto}"}]}]}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "The check XORs the input with 0x42. **Solved** - flag: `flag{x0r_is_not_crypto}`."}]}},
])

ART = {"ok": True, "mime": "text/x-python", "size": 2048, "truncated": False,
       "b64": _b64("#!/usr/bin/env python3\nfrom pwn import *\np = process('./chal')\n# tcache poison -> __free_hook = system\n")}


def _payload_b64(command, marker):
    """Pull the base64 the steering commands pipe through `base64 -d`."""
    m = re.search(rf"printf %s (\S+) \| base64 -d{re.escape(marker)}", command)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1)).decode("utf-8", "replace")
    except Exception:
        return ""


class FakeClient:
    """Stateful fake: status markers and sent/queued messages persist and show up
    in the next snapshot, so every action button has a visible effect."""

    def __init__(self, backend: str):
        self.backend = backend
        self.status = None            # explicit_status dict (Mark solved/blocked)
        self.events = []              # ["queued: …", "sent: …"] shown in the pane

    def exec(self, command, *, stdin=b"", timeout=30):
        # --- steering side effects (persist for the next snapshot) ---
        if "ui-status.json" in command and "rm -f" in command:
            self.status = None
            return ExecResult(0)
        if "ui-status.json" in command:
            txt = _payload_b64(command, " > /home/ctf/run/ui-status.json")
            try:
                self.status = json.loads(txt) if txt else None
            except Exception:
                self.status = None
            return ExecResult(0)
        if "inject.queue" in command:
            self.events.append("queued: " + (_payload_b64(command, " >> /home/ctf/run/inject.queue") or "").strip())
            return ExecResult(0)
        if "send-keys" in command and "base64 -d" in command:
            self.events.append("sent: " + (_payload_b64(command, ")") or "").strip())
            return ExecResult(0)

        # --- reads ---
        if "python3 -" in command:
            m = re.search(r"printf %s (\S+) \|", command)
            src = base64.b64decode(m.group(1)).decode() if m else ""
            if "ART_DIR" in src:
                return ExecResult(0, stdout=json.dumps(ART).encode())
            if "PATTERN" in src:  # transcript fetch — choose by backend's glob
                body = CLAUDE_SESSION if ".claude" in src else CODEX_ROLLOUT
                return ExecResult(0, stdout=json.dumps({"file": "session.jsonl", "b64": _b64(body)}).encode())
            backend = "claude-code" if ".claude" in src else "codex"
            pane = ("● flag{x0r_is_not_crypto}  found\n" if backend == "claude-code"
                    else "$ ./exploit.py\n[+] got shell\n$ cat flag.txt\nflag{clanker_demo}\n")
            extra = ("\n--- operator ---\n" + "\n".join(self.events) + "\n") if self.events else ""
            return ExecResult(0, stdout=json.dumps(
                _snap(pane, "## Notes\nSolved.", backend, status=self.status, extra_pane=extra)).encode())
        return ExecResult(0)

    def download_file(self, p, *, timeout=60):
        return b"(demo file)"


def main():
    tmp = Path(mkdtemp())
    (tmp / "runs").mkdir()
    runs = [
        ("20260603-131503", "ctfvm-heap-20260603-131503", "codex", "167.99.75.28"),
        ("20260603-140210", "ctfvm-rev-20260603-140210", "claude-code", "139.59.100.124"),
    ]
    for rid, inst, backend, ip in runs:
        (tmp / "runs" / f"{rid}.json").write_text(json.dumps({
            "provider": "digitalocean", "run_id": rid, "instance": inst, "zone": "sgp1",
            "project": "digitalocean", "ip": ip, "agent_backend": backend,
            "started_at": "2026-06-03T13:15:03Z", "control_host": ip, "control_port": "443",
            "control_user": "u", "control_password": "pw"}))

    by_backend = {b: FakeClient(b) for _, _, b, _ in runs}
    reg = RunRegistry(runs_dir=tmp / "runs", state_file=tmp / "cur.json", status=lambda r: "active")
    svc = UiService(registry=reg, providers=object(),
                    client_factory=lambda rec: by_backend.get(rec.agent_backend, FakeClient("codex")))
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8800
    print(f"clanker UI preview (seeded data) on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(App(svc))).serve_forever()


if __name__ == "__main__":
    main()
