"""Claude Code agent backend.

Renders ``.claude/settings.json`` + ``.mcp.json`` + ``.claude/agents/*.md`` and
authenticates via an injected ``CLAUDE_CODE_OAUTH_TOKEN`` (from
``claude setup-token``) or ``ANTHROPIC_API_KEY`` — never by copying credential
files, which are keychain/machine-bound (see docs/AGENTS.md §2).
"""

from __future__ import annotations

import json

from .base import AgentBackend, AgentConfigSpec, AuthMaterial, McpServer, StagedFile


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def _mcp_entry(server: McpServer) -> dict:
    if server.kind == "http":
        return {"type": "http", "url": server.url}
    entry: dict = {"type": "stdio", "command": server.command}
    if server.args:
        entry["args"] = list(server.args)
    if server.env:
        entry["env"] = {k: v for k, v in server.env}
    return entry


class ClaudeCodeBackend(AgentBackend):
    name = "claude-code"
    display_name = "Claude Code"
    default_model = ""  # let Claude use its default unless the operator pins one
    transcript_glob = ".claude/projects/**/*.jsonl"
    transcript_recursive = True

    # --- auth --------------------------------------------------------------
    def materialize_auth(self, settings) -> AuthMaterial:  # noqa: ANN001
        wipe = ["/home/ctf/run/.claude"]
        token = settings.get("claude_oauth_token", env_var="CLAUDE_CODE_OAUTH_TOKEN", default="")
        if token:
            return AuthMaterial(
                container_env={"CLAUDE_CODE_OAUTH_TOKEN": str(token)},
                wipe_remote_paths=wipe,
                note="Claude subscription OAuth token (from `claude setup-token`).",
            )
        api_key = settings.get("anthropic_api_key", env_var="ANTHROPIC_API_KEY", default="")
        if api_key:
            return AuthMaterial(
                container_env={"ANTHROPIC_API_KEY": str(api_key)},
                wipe_remote_paths=wipe,
                note="Claude console API-key mode.",
            )
        # No credentials yet — start should refuse with AUTH_REQUIRED.
        return AuthMaterial(
            container_env={},
            wipe_remote_paths=wipe,
            authenticated=False,
            note="No Claude credentials. Run `clanker auth claude` (runs `claude setup-token`).",
        )

    # --- config ------------------------------------------------------------
    def render_config(self, spec: AgentConfigSpec) -> list[StagedFile]:
        staged: list[StagedFile] = []

        settings_json: dict = {
            "permissions": {"defaultMode": "bypassPermissions" if spec.auto_allow else "default"},
        }
        if spec.model:
            settings_json["model"] = spec.model
        staged.append(
            StagedFile(
                remote_relpath=".claude/settings.json",
                content=json.dumps(settings_json, indent=2) + "\n",
                mode="600",
            )
        )

        if spec.mcp_servers:
            mcp_json = {"mcpServers": {s.name: _mcp_entry(s) for s in spec.mcp_servers}}
            staged.append(
                StagedFile(
                    remote_relpath=".mcp.json",
                    content=json.dumps(mcp_json, indent=2) + "\n",
                    mode="600",
                )
            )

        for role in spec.roles:
            frontmatter = [
                "---",
                f"name: {_kebab(role.name)}",
                # json.dumps -> a double-quoted, escaped scalar that is valid YAML
                # even when the description contains ':', newlines, or '---'.
                f"description: {json.dumps(role.description)}",
            ]
            if role.read_only:
                frontmatter.append("tools: Read, Grep, Glob, Bash")
            frontmatter.append("---")
            body = "\n".join(frontmatter) + "\n\n" + role.instructions.rstrip("\n") + "\n"
            staged.append(
                StagedFile(remote_relpath=f".claude/agents/{_kebab(role.name)}.md", content=body, mode="600")
            )
        return staged

    # --- launch ------------------------------------------------------------
    def supervisor_launch_cmd(self, spec: AgentConfigSpec) -> str:
        parts = ["claude"]
        if spec.auto_allow:
            parts += ["--permission-mode", "bypassPermissions"]
        if spec.model:
            parts += ["--model", spec.model]
        return " ".join(parts)
