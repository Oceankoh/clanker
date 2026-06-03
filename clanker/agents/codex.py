"""Codex agent backend — reproduces today's on-VM Codex setup.

Renders ``config.toml`` + ``roles/*.toml`` (into ``.codex/``), syncs the local
``~/.codex`` OAuth session, and launches
``codex --no-alt-screen --enable multi_agent --ask-for-approval never
--sandbox danger-full-access`` exactly as ``runner/supervisor.sh`` does today.
"""

from __future__ import annotations

from pathlib import Path

from .base import AgentBackend, AgentConfigSpec, AuthMaterial, LocalAuthFile, McpServer, StagedFile


def _toml_basic(value: str) -> str:
    """Escape a TOML basic (double-quoted) string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{_toml_basic(v)}"' for v in values) + "]"


def _render_mcp_block(server: McpServer) -> str:
    head = f"[mcp_servers.{server.name}]"
    if server.kind == "http":
        return f'{head}\nurl = "{_toml_basic(server.url)}"\n'
    lines = [head, f'command = "{_toml_basic(server.command)}"']
    if server.args:
        lines.append(f"args = {_toml_array(server.args)}")
    for k, v in server.env:
        lines.append(f'env.{k} = "{_toml_basic(v)}"')
    return "\n".join(lines) + "\n"


class CodexBackend(AgentBackend):
    name = "codex"
    display_name = "Codex CLI"
    default_model = "gpt-5.5"
    # Codex nests active sessions by date: .codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    transcript_glob = ".codex/sessions/**/rollout-*.jsonl"
    transcript_recursive = True

    # --- auth --------------------------------------------------------------
    def materialize_auth(self, settings) -> AuthMaterial:  # noqa: ANN001
        wipe = ["/home/ctf/run/.codex", "/home/ctf/.codex"]
        base_env = {"CODEX_HOME": "/workspace/.codex"}
        no_sync = str(settings.get("no_auth_sync", default="") or "").strip() in ("1", "true", "True")

        # Prefer the local Codex session unless the operator opted out with
        # no_auth_sync. We deliberately do NOT switch to API-key mode merely
        # because OPENAI_API_KEY happens to be exported in the operator's shell
        # (that would silently skip the session sync the bash path always did).
        if not no_sync:
            codex_home = Path(str(settings.get("codex_home", default=str(Path.home() / ".codex"))))
            local_files: list[LocalAuthFile] = []
            for rel in ("auth.json", "installation_id"):
                src = codex_home / rel
                if src.exists():
                    local_files.append(
                        LocalAuthFile(local_path=str(src), remote_relpath=f".codex/{rel}", mode="600")
                    )
            if local_files:
                return AuthMaterial(container_env=base_env, local_files=local_files, wipe_remote_paths=wipe)

        api_key = settings.get("openai_api_key", env_var="OPENAI_API_KEY", default="")
        if api_key:
            return AuthMaterial(
                container_env={**base_env, "OPENAI_API_KEY": str(api_key)},
                wipe_remote_paths=wipe,
                note="Codex API-key mode (no session sync).",
            )
        return AuthMaterial(
            container_env=base_env,
            wipe_remote_paths=wipe,
            authenticated=False,
            note="No Codex credentials. Run `codex login`, or set OPENAI_API_KEY.",
        )

    # --- config ------------------------------------------------------------
    def render_config(self, spec: AgentConfigSpec) -> list[StagedFile]:
        lines: list[str] = []
        if spec.model:
            lines.append(f'model = "{_toml_basic(spec.model)}"')
        lines.append(f'model_reasoning_effort = "{_toml_basic(spec.reasoning_effort)}"')
        lines.append("")

        for path in spec.trusted_paths:
            lines.append(f'[projects."{_toml_basic(path)}"]')
            lines.append('trust_level = "trusted"')
            lines.append("")

        if spec.multi_agent:
            lines.append("[features]")
            lines.append("multi_agent = true")
            lines.append("")

        for role in spec.roles:
            lines.append(f"[agents.{role.name}]")
            lines.append(f'description = "{_toml_basic(role.description)}"')
            lines.append(f'config_file = "roles/{role.name}.toml"')
            lines.append("")

        for server in spec.mcp_servers:
            lines.append(_render_mcp_block(server))

        config_toml = "\n".join(lines).rstrip() + "\n"
        staged = [StagedFile(remote_relpath=".codex/config.toml", content=config_toml, mode="600")]

        for role in spec.roles:
            role_lines = [f'model_reasoning_effort = "{_toml_basic(role.reasoning_effort)}"']
            if role.sandbox:
                role_lines.append(f'sandbox_mode = "{_toml_basic(role.sandbox)}"')
            role_lines.append("")
            role_lines.append("developer_instructions = \"\"\"")
            # Escape so no embedded `"""` or trailing backslash can break the
            # triple-quoted TOML string (multiline basic strings honor escapes).
            role_lines.append(_toml_basic(role.instructions.rstrip("\n")))
            role_lines.append('"""')
            staged.append(
                StagedFile(
                    remote_relpath=f".codex/roles/{role.name}.toml",
                    content="\n".join(role_lines) + "\n",
                    mode="600",
                )
            )
        return staged

    # --- launch ------------------------------------------------------------
    def supervisor_launch_cmd(self, spec: AgentConfigSpec) -> str:
        parts = ["codex", "--no-alt-screen"]
        if spec.multi_agent:
            parts += ["--enable", "multi_agent"]
        if spec.auto_allow:
            parts += ["--ask-for-approval", "never", "--sandbox", "danger-full-access"]
        return " ".join(parts)
