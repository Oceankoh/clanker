"""Agent transcript parsing — turn a backend's session JSONL into a uniform
chat-style event stream (messages, reasoning, tool calls, tool results).

Schemas were derived from real Codex rollout and Claude session transcripts:

- Codex rollout JSONL: one object per line ``{timestamp, type, payload}``.
  ``type=="response_item"`` carries the conversation in ``payload.type``:
  message / reasoning / function_call / function_call_output /
  custom_tool_call(/_output).
- Claude session JSONL: lines with ``type`` user/assistant and a
  ``message.content`` list of blocks: text / thinking / tool_use / tool_result.

Both map onto ``TranscriptEvent`` so the UI renders one chat view per backend.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

MAX_EVENTS = 400
_TOOL_INPUT_CAP = 600
_RESULT_CAP = 4000


@dataclass
class TranscriptEvent:
    role: str            # user | assistant | system | tool
    kind: str            # message | reasoning | tool_call | tool_result
    text: str = ""
    tool: str = ""       # tool name (tool_call)
    tool_input: str = ""  # summarized args (tool_call)
    is_error: bool = False
    ts: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _loads(line: str):
    try:
        o = json.loads(line)
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def _clip(s: str, n: int) -> str:
    s = s if isinstance(s, str) else json.dumps(s, default=str)
    return s if len(s) <= n else s[:n] + " …"


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def parse_codex_rollout(text: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    for line in text.splitlines():
        o = _loads(line)
        if not o or o.get("type") != "response_item":
            continue
        ts = str(o.get("timestamp", "") or "")
        p = o.get("payload") or {}
        pt = p.get("type")

        if pt == "message":
            txt = "".join(
                el.get("text", "") for el in (p.get("content") or [])
                if isinstance(el, dict) and el.get("text")
            ).strip()
            if txt:
                events.append(TranscriptEvent(role=str(p.get("role", "assistant")), kind="message", text=txt, ts=ts))
        elif pt == "reasoning":
            summary = p.get("summary") or []
            txt = "".join(el.get("text", "") for el in summary if isinstance(el, dict)).strip()
            events.append(TranscriptEvent(role="assistant", kind="reasoning", text=txt or "(thinking)", ts=ts))
        elif pt in ("function_call", "custom_tool_call"):
            events.append(TranscriptEvent(
                role="assistant", kind="tool_call",
                tool=str(p.get("name", "") or "tool"),
                tool_input=_clip(p.get("arguments") or p.get("input") or "", _TOOL_INPUT_CAP), ts=ts,
            ))
        elif pt in ("function_call_output", "custom_tool_call_output"):
            out = p.get("output")
            if isinstance(out, dict):
                out = out.get("content") or out.get("output") or out
            events.append(TranscriptEvent(role="tool", kind="tool_result", text=_clip(out or "", _RESULT_CAP), ts=ts))
    return events[-MAX_EVENTS:]


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def parse_claude_session(text: str) -> list[TranscriptEvent]:
    events: list[TranscriptEvent] = []
    for line in text.splitlines():
        o = _loads(line)
        if not o or o.get("type") not in ("user", "assistant"):
            continue
        ts = str(o.get("timestamp", "") or "")
        role = o.get("type")
        msg = o.get("message") or {}
        content = msg.get("content")

        if isinstance(content, str):
            if content.strip():
                events.append(TranscriptEvent(role=role, kind="message", text=content.strip(), ts=ts))
            continue
        for el in content or []:
            if not isinstance(el, dict):
                continue
            bt = el.get("type")
            if bt == "text" and el.get("text", "").strip():
                events.append(TranscriptEvent(role=role, kind="message", text=el["text"].strip(), ts=ts))
            elif bt == "thinking":
                events.append(TranscriptEvent(role="assistant", kind="reasoning",
                                              text=_clip(el.get("thinking", "") or "(thinking)", _RESULT_CAP), ts=ts))
            elif bt == "tool_use":
                events.append(TranscriptEvent(role="assistant", kind="tool_call",
                                              tool=str(el.get("name", "") or "tool"),
                                              tool_input=_clip(el.get("input") or "", _TOOL_INPUT_CAP), ts=ts))
            elif bt == "tool_result":
                rc = el.get("content")
                if isinstance(rc, list):
                    rc = "".join(b.get("text", "") for b in rc if isinstance(b, dict))
                events.append(TranscriptEvent(role="tool", kind="tool_result",
                                              text=_clip(rc or "", _RESULT_CAP),
                                              is_error=bool(el.get("is_error")), ts=ts))
    return events[-MAX_EVENTS:]


def parse_transcript(backend: str, text: str) -> list[TranscriptEvent]:
    if backend == "claude-code":
        return parse_claude_session(text)
    return parse_codex_rollout(text)
