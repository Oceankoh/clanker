"""Agent backend layer + factory."""

from __future__ import annotations

from .base import (
    AgentBackend,
    AgentConfigSpec,
    AuthMaterial,
    LocalAuthFile,
    McpServer,
    StagedFile,
    SubagentRole,
    default_agent_spec,
)
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend

__all__ = [
    "AgentBackend",
    "AgentConfigSpec",
    "AuthMaterial",
    "LocalAuthFile",
    "McpServer",
    "StagedFile",
    "SubagentRole",
    "default_agent_spec",
    "CodexBackend",
    "ClaudeCodeBackend",
    "build_agent_backend",
    "SUPPORTED_BACKENDS",
]

SUPPORTED_BACKENDS = ("codex", "claude-code")

_ALIASES = {
    "codex": "codex",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "claudecode": "claude-code",
    "claude_code": "claude-code",
}


def build_agent_backend(name: str | None) -> AgentBackend:
    key = _ALIASES.get(str(name or "codex").strip().lower())
    if key == "codex":
        return CodexBackend()
    if key == "claude-code":
        return ClaudeCodeBackend()
    raise KeyError(f"unknown agent backend: {name!r} (supported: {', '.join(SUPPORTED_BACKENDS)})")
