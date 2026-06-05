"""AgentBackend abstraction — the centerpiece of the refactor.

Makes the in-VM AI agent pluggable. ``CloudProvider`` answers "where does work
run?"; ``AgentBackend`` answers "what agent runs, and how do we authenticate,
configure, and launch it?".

A backend-neutral ``AgentConfigSpec`` (model, MCP servers, subagent roles) is
rendered to whichever on-VM config a backend needs (Codex ``config.toml`` vs
Claude ``settings.json`` + ``.mcp.json`` + ``.claude/agents``). This module owns
the neutral spec + the default spec; the two backends own the rendering.

Phase 3a implements the pure, testable surface (auth/config/launch). The
VM-runtime methods (spawn/detect subagents, steer, frontend features) are
declared here as the documented contract and land in the Phase 3 follow-on —
see docs/AGENTS.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Neutral specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class McpServer:
    """A backend-neutral MCP server declaration."""

    name: str
    kind: str  # "stdio" | "http"
    command: str = ""
    args: tuple[str, ...] = ()
    url: str = ""
    env: tuple[tuple[str, str], ...] = ()  # frozen mapping


@dataclass(frozen=True)
class SubagentRole:
    """A reusable subagent role (Codex 'agent' / Claude '.claude/agents' entry)."""

    name: str
    description: str
    instructions: str
    reasoning_effort: str = "high"
    sandbox: str = ""          # codex sandbox_mode; "" -> backend default
    read_only: bool = False    # generalizes 'sandbox_mode = read-only'


@dataclass
class AgentConfigSpec:
    """Everything needed to render an agent's on-VM configuration."""

    model: str = ""
    reasoning_effort: str = "xhigh"
    multi_agent: bool = True
    auto_allow: bool = True  # run non-interactively (no approval prompts)
    mcp_servers: list[McpServer] = field(default_factory=list)
    roles: list[SubagentRole] = field(default_factory=list)
    trusted_paths: list[str] = field(default_factory=lambda: ["/workspace", "/workspace/challenge"])


@dataclass
class StagedFile:
    """A file to write into the run directory on the VM.

    ``remote_relpath`` is relative to the run dir (``/home/ctf/run``)."""

    remote_relpath: str
    content: str
    mode: str = ""  # octal string, e.g. "600"; "" -> leave default


@dataclass
class LocalAuthFile:
    """A local credential file to copy onto the VM (Codex session sync)."""

    local_path: str
    remote_relpath: str
    mode: str = "600"


@dataclass
class AuthMaterial:
    """How a backend authenticates on the VM.

    ``container_env`` is injected into the toolbox container; ``local_files`` are
    copied from the operator's machine; ``wipe_remote_paths`` are shredded on
    ``destroy``. ``authenticated`` says whether real credentials were found —
    callers must check this rather than inferring it from ``container_env``
    (which always carries placeholders like ``CODEX_HOME``)."""

    container_env: dict[str, str] = field(default_factory=dict)
    local_files: list[LocalAuthFile] = field(default_factory=list)
    wipe_remote_paths: list[str] = field(default_factory=list)
    authenticated: bool = True
    note: str = ""  # human-facing note (e.g. "run `clanker auth claude` first")


# --- the two default subagent roles (shared across backends) ----------------

EXPLOIT_TESTER = SubagentRole(
    name="exploit_tester",
    description=(
        "Execution-focused exploit hypothesis tester. Use when validating a single exploit path, "
        "gadget chain, primitive, or challenge segment in parallel."
    ),
    instructions=(
        "You are exploit_tester.\n"
        "Focus on validating one concrete exploit hypothesis.\n"
        "Use reproducible commands and store evidence under "
        "/workspace/artifacts/subagents/exploit-tester/.\n"
        "Return a strict verdict: confirmed, rejected, or inconclusive.\n"
        "If inconclusive, provide one concrete next test.\n"
    ),
    sandbox="danger-full-access",
)

DOCS_RESEARCHER = SubagentRole(
    name="docs_researcher",
    description=(
        "Read-heavy docs and behavior validation role. Use for command semantics, protocol details, "
        "API usage, and minimal reproducible behavior checks before risky actions."
    ),
    instructions=(
        "You are docs_researcher.\n"
        "Answer documentation and behavior questions with authoritative sources and minimal "
        "verification commands.\n"
        "Prefer official docs and built-in --help output.\n"
        "Treat unverified claims as tentative and label them clearly.\n"
        "Return concise recommended next actions for the supervisor.\n"
    ),
    read_only=True,
    sandbox="read-only",
)

GDB_MCP = McpServer(
    name="gdb",
    kind="stdio",
    command="python3",
    args=("/opt/ctf-toolbox/mcp/gdb_mcp.py",),
)


def default_agent_spec(
    *, model: str = "", reasoning_effort: str = "", ida_mcp_url: str = ""
) -> AgentConfigSpec:
    """The platform's default agent configuration: the bundled GDB MCP, the two
    standard subagent roles, multi-agent + auto-allow on, and an optional remote
    IDA MCP when configured.

    ``reasoning_effort`` blank -> the spec default (``xhigh``); a non-empty value
    pins it (mirrors how ``model`` blank means "backend default").

    Note: the legacy ``config.toml`` also pinned a hardcoded ``idaPro`` URL — a
    dev leftover (a specific droplet IP). It is intentionally dropped; IDA is
    wired only via ``ida_mcp_url`` (``CTFVM_DEFAULT_IDA_MCP_URL``)."""
    mcp = [GDB_MCP]
    if ida_mcp_url:
        mcp.append(McpServer(name="ida", kind="http", url=ida_mcp_url))
    spec_kwargs: dict = dict(model=model, mcp_servers=mcp, roles=[EXPLOIT_TESTER, DOCS_RESEARCHER])
    if reasoning_effort:
        spec_kwargs["reasoning_effort"] = reasoning_effort
    return AgentConfigSpec(**spec_kwargs)


# ---------------------------------------------------------------------------
# The backend contract
# ---------------------------------------------------------------------------


_TRANSCRIPT_BODY = r'''
import base64, glob, json, os
files = glob.glob(PATTERN, recursive=RECURSIVE)
newest = max(files, key=os.path.getmtime) if files else ""
data = b""
if newest:
    with open(newest, "rb") as f:
        try:
            f.seek(-TAIL, 2)
        except OSError:
            f.seek(0)
        data = f.read()
print(json.dumps({"file": os.path.basename(newest), "b64": base64.b64encode(data).decode("ascii")}))
'''


class AgentBackend(ABC):
    name: str = ""
    display_name: str = ""
    default_model: str = ""
    cli_binary: str = ""  # the CLI invoked in the container (for readiness display)
    # where this backend's session transcript lives, relative to the run dir
    transcript_glob: str = ""
    transcript_recursive: bool = False

    def transcript_script(self, remote_run_dir: str, *, tail_bytes: int = 400_000) -> str:
        """Remote Python that finds the newest session transcript and tails it,
        emitting ``{file, b64}``. The b64 is parsed by ``transcript.parse_transcript``."""
        pattern = remote_run_dir.rstrip("/") + "/" + self.transcript_glob
        header = (
            f"PATTERN = {pattern!r}\n"
            f"RECURSIVE = {bool(self.transcript_recursive)}\n"
            f"TAIL = {int(tail_bytes)}\n"
        )
        return header + _TRANSCRIPT_BODY

    def build_spec(
        self, *, model: str = "", reasoning_effort: str = "", ida_mcp_url: str = ""
    ) -> AgentConfigSpec:
        return default_agent_spec(
            model=model or self.default_model,
            reasoning_effort=reasoning_effort,
            ida_mcp_url=ida_mcp_url,
        )

    # --- pure, testable surface (Phase 3a) --------------------------------
    @abstractmethod
    def materialize_auth(self, settings) -> AuthMaterial:  # noqa: ANN001
        """Turn local operator credentials into an injectable form."""

    @abstractmethod
    def render_config(self, spec: AgentConfigSpec) -> list[StagedFile]:
        """Render the backend's on-VM config / MCP / subagent-role files."""

    @abstractmethod
    def supervisor_launch_cmd(self, spec: AgentConfigSpec) -> str:
        """The command supervisor.sh runs (inside the toolbox) to start the agent."""

    # --- VM-runtime surface (Phase 3 follow-on) ---------------------------
    def spawn_subagent(self, *args, **kwargs):
        raise NotImplementedError("subagent spawn lands in the Phase 3 follow-on")

    def detect_subagents(self, *args, **kwargs):
        raise NotImplementedError("subagent detection lands in the Phase 3 follow-on")

    def frontend_features(self, *args, **kwargs):
        raise NotImplementedError("frontend features land in Phase 5/6")
