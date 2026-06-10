"""Agent backend layer + a small registry.

Adding an agent backend is: subclass ``AgentBackend``, implement the contract
(``materialize_auth`` / ``render_config`` / ``supervisor_launch_cmd`` +
``transcript_glob``), and ``register_backend(YourBackend, *aliases)``. Nothing
else needs editing — the factory, ``clanker agents``, the spawn form, and the
``--agent`` choices all read the registry.
"""

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
    "AgentBackend", "AgentConfigSpec", "AuthMaterial", "LocalAuthFile", "McpServer",
    "StagedFile", "SubagentRole", "default_agent_spec",
    "CodexBackend", "ClaudeCodeBackend",
    "register_backend", "build_agent_backend", "list_backends", "SUPPORTED_BACKENDS",
]

_REGISTRY: dict[str, type[AgentBackend]] = {}
_ALIASES: dict[str, str] = {}


def register_backend(cls: type[AgentBackend], *aliases: str) -> type[AgentBackend]:
    """Register an agent backend class (idempotent). The canonical name is
    ``cls.name``; ``aliases`` are extra accepted spellings."""
    _REGISTRY[cls.name] = cls
    for alias in (cls.name, *aliases):
        _ALIASES[alias.strip().lower()] = cls.name
    return cls


register_backend(CodexBackend)
register_backend(ClaudeCodeBackend, "claude", "claudecode", "claude_code")


def build_agent_backend(name: str | None) -> AgentBackend:
    key = _ALIASES.get(str(name or "codex").strip().lower())
    if key and key in _REGISTRY:
        return _REGISTRY[key]()
    raise KeyError(f"unknown agent backend: {name!r} (supported: {', '.join(_REGISTRY)})")


def list_backends() -> list[AgentBackend]:
    return [_REGISTRY[n]() for n in _REGISTRY]


def _supported() -> tuple[str, ...]:
    return tuple(_REGISTRY)


# tuple snapshot for argparse choices etc.; use list_backends()/_supported() when dynamic
SUPPORTED_BACKENDS = _supported()
