"""Phase 3b: secret store + stage-agent integration artifact.

    python tests/test_auth_stage.py
    python -m pytest tests/test_auth_stage.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker import commands  # noqa: E402
from clanker.config import Settings  # noqa: E402
from clanker.secretstore import get_secret, set_secret  # noqa: E402


class SecretStore(unittest.TestCase):
    def test_roundtrip_and_perms(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "secrets.json"
            set_secret("claude_oauth_token", "tok-123", path=path)
            self.assertEqual(get_secret("claude_oauth_token", path=path), "tok-123")
            # 0600
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)
            # second key preserves the first
            set_secret("other", "x", path=path)
            self.assertEqual(get_secret("claude_oauth_token", path=path), "tok-123")
            self.assertEqual(get_secret("other", path=path), "x")


class StageAgent(unittest.TestCase):
    def test_codex_payload(self):
        with TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "dotcodex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text("{}")
            (codex_home / "installation_id").write_text("id")
            staging = Path(tmp) / "stage"
            settings = Settings(root=Path(tmp), cli={"codex_home": str(codex_home)})

            rc = commands.cmd_stage_agent("codex", str(staging), settings=settings)
            self.assertEqual(rc, 0)
            self.assertTrue((staging / ".codex/config.toml").exists())
            self.assertTrue((staging / ".codex/roles/exploit_tester.toml").exists())
            self.assertTrue((staging / ".codex/auth.json").exists())  # local session copied
            self.assertEqual((staging / "agent/backend").read_text().strip(), "codex")
            self.assertIn("codex --no-alt-screen", (staging / "agent/launch.cmd").read_text())
            self.assertIn("CODEX_HOME=/workspace/.codex", (staging / "agent/container.env").read_text())
            self.assertIn("/home/ctf/run/.codex", (staging / "agent/wipe-paths.txt").read_text())

    def test_claude_payload_with_token(self):
        with TemporaryDirectory() as tmp:
            staging = Path(tmp) / "stage"
            settings = Settings(root=Path(tmp), cli={"claude_oauth_token": "tok"})
            rc = commands.cmd_stage_agent("claude-code", str(staging), settings=settings)
            self.assertEqual(rc, 0)
            self.assertTrue((staging / ".claude/settings.json").exists())
            mcp = json.loads((staging / ".mcp.json").read_text())["mcpServers"]
            self.assertIn("gdb", mcp)
            self.assertEqual((staging / "agent/backend").read_text().strip(), "claude-code")
            env = (staging / "agent/container.env").read_text()
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=tok", env)
            self.assertIn("claude --permission-mode bypassPermissions", (staging / "agent/launch.cmd").read_text())

    def test_claude_no_creds_is_auth_required(self):
        with TemporaryDirectory() as tmp:
            staging = Path(tmp) / "stage"
            settings = Settings(root=Path(tmp), cli={})  # empty root -> no .env/secrets
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                os.environ.pop("ANTHROPIC_API_KEY", None)
                rc = commands.cmd_stage_agent("claude-code", str(staging), settings=settings)
            self.assertEqual(rc, 3)  # AUTH_REQUIRED


if __name__ == "__main__":
    unittest.main(verbosity=2)
