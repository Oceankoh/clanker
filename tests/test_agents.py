"""Phase 3: agent backend layer (config/auth/launch rendering).

    python tests/test_agents.py
    python -m pytest tests/test_agents.py
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker.agents import build_agent_backend  # noqa: E402
from clanker.agents.base import DOCS_RESEARCHER, EXPLOIT_TESTER, AgentConfigSpec, default_agent_spec  # noqa: E402
from clanker.config import Settings  # noqa: E402


def _staged(backend, spec):
    return {sf.remote_relpath: sf for sf in backend.render_config(spec)}


class CodexRender(unittest.TestCase):
    def setUp(self):
        self.backend = build_agent_backend("codex")
        self.spec = self.backend.build_spec()

    def test_config_toml_core(self):
        files = _staged(self.backend, self.spec)
        cfg = files[".codex/config.toml"].content
        self.assertIn('model = "gpt-5.5"', cfg)
        self.assertIn("multi_agent = true", cfg)
        self.assertIn('[projects."/workspace"]', cfg)
        self.assertIn("[agents.exploit_tester]", cfg)
        self.assertIn('config_file = "roles/exploit_tester.toml"', cfg)
        self.assertIn("[mcp_servers.gdb]", cfg)
        self.assertIn('command = "python3"', cfg)
        self.assertIn('args = ["/opt/ctf-toolbox/mcp/gdb_mcp.py"]', cfg)
        # default has NO ida (and never the legacy hardcoded idaPro IP)
        self.assertNotIn("[mcp_servers.ida]", cfg)
        self.assertNotIn("idaPro", cfg)

    def test_role_files(self):
        files = _staged(self.backend, self.spec)
        role = files[".codex/roles/exploit_tester.toml"].content
        self.assertIn('sandbox_mode = "danger-full-access"', role)
        self.assertIn("developer_instructions =", role)
        self.assertIn("You are exploit_tester.", role)
        ro = files[".codex/roles/docs_researcher.toml"].content
        self.assertIn('sandbox_mode = "read-only"', ro)
        # mode hardening preserved
        self.assertEqual(files[".codex/config.toml"].mode, "600")

    def test_reasoning_effort_defaults_to_xhigh(self):
        cfg = _staged(self.backend, self.spec)[".codex/config.toml"].content
        self.assertIn('model_reasoning_effort = "xhigh"', cfg)

    def test_reasoning_effort_override(self):
        spec = self.backend.build_spec(reasoning_effort="medium")
        cfg = _staged(self.backend, spec)[".codex/config.toml"].content
        self.assertIn('model_reasoning_effort = "medium"', cfg)
        self.assertNotIn('model_reasoning_effort = "xhigh"', cfg)

    def test_ida_via_env_only(self):
        spec = self.backend.build_spec(ida_mcp_url="http://10.0.0.5:8745/mcp")
        cfg = _staged(self.backend, spec)[".codex/config.toml"].content
        self.assertIn("[mcp_servers.ida]", cfg)
        self.assertIn('url = "http://10.0.0.5:8745/mcp"', cfg)

    def test_launch_cmd(self):
        cmd = self.backend.supervisor_launch_cmd(self.spec)
        self.assertEqual(
            cmd,
            "codex --no-alt-screen --enable multi_agent --ask-for-approval never --sandbox danger-full-access",
        )

    def test_auth_sync_stages_local_files(self):
        with TemporaryDirectory() as home:
            codex_home = Path(home) / ".codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text("{}")
            (codex_home / "installation_id").write_text("abc")
            settings = Settings(cli={"codex_home": str(codex_home)})
            auth = self.backend.materialize_auth(settings)
            staged = {f.remote_relpath for f in auth.local_files}
            self.assertEqual(staged, {".codex/auth.json", ".codex/installation_id"})
            self.assertEqual(auth.container_env.get("CODEX_HOME"), "/workspace/.codex")
            self.assertIn("/home/ctf/run/.codex", auth.wipe_remote_paths)

    def test_auth_api_key_mode_requires_no_sync(self):
        # api-key mode only when the operator opts out of session sync
        settings = Settings(cli={"openai_api_key": "sk-test", "no_auth_sync": "1"})
        auth = self.backend.materialize_auth(settings)
        self.assertEqual(auth.container_env.get("OPENAI_API_KEY"), "sk-test")
        self.assertEqual(auth.local_files, [])
        self.assertTrue(auth.authenticated)

    def test_session_wins_over_ambient_openai_api_key(self):
        with TemporaryDirectory() as home:
            codex_home = Path(home) / ".codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text("{}")
            settings = Settings(cli={"codex_home": str(codex_home), "openai_api_key": "sk-ambient"})
            auth = self.backend.materialize_auth(settings)
            # session sync wins; ambient key does NOT flip to api-key mode
            self.assertTrue(any(f.remote_relpath == ".codex/auth.json" for f in auth.local_files))
            self.assertNotIn("OPENAI_API_KEY", auth.container_env)

    def test_no_creds_is_unauthenticated(self):
        with TemporaryDirectory() as home:
            empty = Path(home) / ".codex"  # does not exist -> no session
            settings = Settings(cli={"codex_home": str(empty)})
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OPENAI_API_KEY", None)
                auth = self.backend.materialize_auth(settings)
            self.assertFalse(auth.authenticated)
            self.assertEqual(auth.local_files, [])

    def test_role_toml_escaping_is_robust(self):
        # a role whose instructions contain `"""` and a trailing backslash must
        # still render valid TOML
        nasty = replace(EXPLOIT_TESTER, instructions='say """ and end with \\')
        spec = AgentConfigSpec(model="m", roles=[nasty], mcp_servers=[])
        cfg = _staged(self.backend, spec)
        tomllib.loads(cfg[".codex/roles/exploit_tester.toml"].content)  # must not raise

    def test_roles_match_committed_toml(self):
        # drift guard: base.py role constants must match the on-disk codex role
        # configs (images/ctf-toolbox/codex-config/roles/*.toml)
        roles_dir = REPO_ROOT / "images/ctf-toolbox/codex-config/roles"
        for role in (EXPLOIT_TESTER, DOCS_RESEARCHER):
            data = tomllib.loads((roles_dir / f"{role.name}.toml").read_text())
            self.assertEqual(data.get("sandbox_mode", ""), role.sandbox, role.name)
            self.assertEqual(
                data["developer_instructions"].strip(), role.instructions.strip(), role.name
            )


class ClaudeRender(unittest.TestCase):
    def setUp(self):
        self.backend = build_agent_backend("claude-code")
        self.spec = self.backend.build_spec(ida_mcp_url="http://10.0.0.5:8745/mcp")

    def test_settings_json(self):
        files = _staged(self.backend, self.spec)
        settings = json.loads(files[".claude/settings.json"].content)
        self.assertEqual(settings["permissions"]["defaultMode"], "bypassPermissions")

    def test_mcp_json_stdio_and_http(self):
        files = _staged(self.backend, self.spec)
        mcp = json.loads(files[".mcp.json"].content)["mcpServers"]
        self.assertEqual(mcp["gdb"]["type"], "stdio")
        self.assertEqual(mcp["gdb"]["command"], "python3")
        self.assertEqual(mcp["gdb"]["args"], ["/opt/ctf-toolbox/mcp/gdb_mcp.py"])
        self.assertEqual(mcp["ida"]["type"], "http")
        self.assertEqual(mcp["ida"]["url"], "http://10.0.0.5:8745/mcp")

    def test_agents_md(self):
        files = _staged(self.backend, self.spec)
        self.assertIn(".claude/agents/exploit-tester.md", files)
        md = files[".claude/agents/exploit-tester.md"].content
        self.assertIn("name: exploit-tester", md)
        self.assertIn("You are exploit_tester.", md)
        ro = files[".claude/agents/docs-researcher.md"].content
        self.assertIn("tools: Read, Grep, Glob, Bash", ro)  # read-only role

    def test_launch_cmd(self):
        self.assertEqual(self.backend.supervisor_launch_cmd(self.spec), "claude --dangerously-skip-permissions")
        with_model = self.backend.build_spec(model="opus")
        self.assertEqual(
            self.backend.supervisor_launch_cmd(with_model),
            "claude --dangerously-skip-permissions --model opus",
        )

    def test_claude_json_preseeds_first_run_gates(self):
        files = _staged(self.backend, self.spec)
        cj = json.loads(files[".claude.json"].content)
        self.assertTrue(cj["hasCompletedOnboarding"])
        self.assertTrue(cj["bypassPermissionsModeAccepted"])
        proj = cj["projects"]["/workspace"]
        self.assertTrue(proj["hasTrustDialogAccepted"])
        self.assertIn("gdb", proj["enabledMcpjsonServers"])

    def test_auth_token_and_apikey_and_missing(self):
        # isolate from the repo's real .ctfvm/secrets.json and ambient env
        with TemporaryDirectory() as root, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)

            def S(**cli):
                return Settings(root=Path(root), cli=cli)

            a = self.backend.materialize_auth(S(claude_oauth_token="tok"))
            self.assertEqual(a.container_env.get("CLAUDE_CODE_OAUTH_TOKEN"), "tok")
            self.assertEqual(a.local_files, [])  # never copies credential files

            b = self.backend.materialize_auth(S(anthropic_api_key="sk"))
            self.assertEqual(b.container_env.get("ANTHROPIC_API_KEY"), "sk")

            c = self.backend.materialize_auth(S())
            self.assertEqual(c.container_env, {})  # forces AUTH_REQUIRED at start
            self.assertIn("clanker auth claude", c.note)


class Registry(unittest.TestCase):
    def test_normalization_and_unknown(self):
        self.assertEqual(build_agent_backend("codex").name, "codex")
        self.assertEqual(build_agent_backend("claude").name, "claude-code")
        self.assertEqual(build_agent_backend("claude_code").name, "claude-code")
        self.assertEqual(build_agent_backend(None).name, "codex")  # default
        with self.assertRaises(KeyError):
            build_agent_backend("gemini")

    def test_register_new_backend_without_editing_factory(self):
        from clanker.agents import CodexBackend, list_backends, register_backend
        from clanker.agents import _ALIASES, _REGISTRY  # noqa: PLC2701

        class DummyBackend(CodexBackend):
            name = "dummy-agent"
            display_name = "Dummy"

        self.addCleanup(lambda: _REGISTRY.pop("dummy-agent", None))
        self.addCleanup(lambda: [_ALIASES.pop(k, None) for k in ("dummy-agent", "dummy")])

        register_backend(DummyBackend, "dummy")
        self.assertEqual(build_agent_backend("dummy").name, "dummy-agent")
        self.assertIn("dummy-agent", [b.name for b in list_backends()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
