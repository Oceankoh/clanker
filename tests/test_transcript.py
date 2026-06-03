"""Transcript parsing — Codex rollout + Claude session JSONL -> chat events.

Fixtures mirror the real on-disk schemas (verified against live transcripts);
no real transcript content is committed.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker.transcript import parse_claude_session, parse_codex_rollout, parse_transcript  # noqa: E402

CODEX = "\n".join(json.dumps(o) for o in [
    {"type": "session_meta", "payload": {"id": "x"}},
    {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": "solve the chal"}]}},
    {"type": "response_item", "payload": {"type": "reasoning",
        "summary": [{"type": "text", "text": "consider UAF"}]}},
    {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command",
        "call_id": "c1", "arguments": "{\"command\":[\"ls\"]}"}},
    {"type": "event_msg", "payload": {"type": "token_count", "turn_id": "t"}},
    {"type": "response_item", "payload": {"type": "function_call_output",
        "call_id": "c1", "output": "exploit.py\nflag.txt"}},
    {"type": "response_item", "payload": {"type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "found flag{x}"}]}},
])

CLAUDE = "\n".join(json.dumps(o) for o in [
    {"type": "system", "subtype": "init"},
    {"type": "user", "message": {"role": "user", "content": "solve it"}},
    {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "on it"},
        {"type": "thinking", "thinking": "try heap"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
    ]}},
    {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "x", "is_error": True,
         "content": [{"type": "text", "text": "permission denied"}]}]}},
])


class Codex(unittest.TestCase):
    def test_parse(self):
        evs = parse_codex_rollout(CODEX)
        kinds = [(e.kind, e.role) for e in evs]
        self.assertIn(("message", "user"), kinds)
        self.assertIn(("reasoning", "assistant"), kinds)
        self.assertIn(("message", "assistant"), kinds)
        call = next(e for e in evs if e.kind == "tool_call")
        self.assertEqual(call.tool, "exec_command")
        self.assertIn("ls", call.tool_input)
        res = next(e for e in evs if e.kind == "tool_result")
        self.assertIn("exploit.py", res.text)
        # session_meta / event_msg are not events
        self.assertEqual(sum(1 for e in evs if e.kind == "message"), 2)


class Claude(unittest.TestCase):
    def test_parse(self):
        evs = parse_claude_session(CLAUDE)
        self.assertEqual(evs[0].kind, "message")
        self.assertEqual(evs[0].role, "user")
        self.assertTrue(any(e.kind == "message" and e.text == "on it" for e in evs))
        self.assertTrue(any(e.kind == "reasoning" and "heap" in e.text for e in evs))
        call = next(e for e in evs if e.kind == "tool_call")
        self.assertEqual(call.tool, "Bash")
        res = next(e for e in evs if e.kind == "tool_result")
        self.assertTrue(res.is_error)
        self.assertIn("permission denied", res.text)


class Dispatch(unittest.TestCase):
    def test_backend_dispatch(self):
        self.assertTrue(parse_transcript("claude-code", CLAUDE))
        self.assertTrue(parse_transcript("codex", CODEX))
        # unknown backend falls back to codex parser (no crash)
        self.assertEqual(parse_transcript("other", ""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
