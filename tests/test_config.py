"""Config: schema-driven env-addressability + provenance + `config show`."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker import commands  # noqa: E402
from clanker.config import SETTINGS_SCHEMA, Settings  # noqa: E402
from clanker.server.service import UiService  # noqa: E402


def _settings(tmp, env="", secrets=None, cli=None):
    root = Path(tmp)
    if env:
        (root / ".env").write_text(env)
    if secrets is not None:
        (root / ".ctfvm").mkdir(parents=True, exist_ok=True)
        (root / ".ctfvm" / "secrets.json").write_text(json.dumps(secrets))
    return Settings(root=root, cli=cli or {})


class Provenance(unittest.TestCase):
    def test_precedence_and_source(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CTFVM_AGENT", None)
            os.environ.pop("CTFVM_CODEX_HOME", None)
            s = _settings(tmp, env="CTFVM_AGENT=claude-code\nCTFVM_CODEX_HOME=/custom/.codex\n")
            # .env drives a previously-non-env-addressable key (the gap we fixed)
            self.assertEqual(s.resolve("codex_home"), ("/custom/.codex", ".env"))
            self.assertEqual(s.resolve("agent_backend"), ("claude-code", ".env"))
            # default fallback from the schema
            self.assertEqual(s.resolve("model"), ("", "default"))
            # cli wins over .env
            s2 = _settings(tmp, cli={"agent_backend": "codex"})
            self.assertEqual(s2.resolve("agent_backend")[0], "codex")
            self.assertEqual(s2.resolve("agent_backend")[1], "cli")

    def test_secret_source(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            s = _settings(tmp, secrets={"claude_oauth_token": "tok"})
            self.assertEqual(s.resolve("claude_oauth_token"), ("tok", "secret"))

    def test_env_overrides_secret(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "envtok"}):
            s = _settings(tmp, secrets={"claude_oauth_token": "sectok"})
            self.assertEqual(s.resolve("claude_oauth_token"), ("envtok", "env"))

    def test_effective_covers_schema(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(len(_settings(tmp).effective()), len(SETTINGS_SCHEMA))


class ConfigShow(unittest.TestCase):
    def test_redacts_secrets(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            s = _settings(tmp, env="CTFVM_AGENT=claude-code\n", secrets={"claude_oauth_token": "supersecret"})
            buf = io.StringIO()
            with redirect_stdout(buf):
                commands.cmd_config_show(settings=s)
            out = buf.getvalue()
            self.assertIn("[agent]", out)
            self.assertIn("CTFVM_AGENT", out)
            self.assertIn("claude-code", out)
            self.assertIn("***set***", out)       # secret marked set
            self.assertNotIn("supersecret", out)   # never printed


class Profiles(unittest.TestCase):
    def test_profile_overlay_and_precedence(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            from clanker.secretstore import get_profile, list_profiles, set_profile
            path = Path(tmp) / "secrets.json"
            set_profile("alice", {"backend": "claude-code", "claude_oauth_token": "alice-tok"}, path=path)
            set_profile("bob", {"backend": "codex", "openai_api_key": "sk-bob", "no_auth_sync": "1"}, path=path)
            self.assertEqual(sorted(list_profiles(path=path)), ["alice", "bob"])

            s = Settings(root=Path(tmp), profile=get_profile("alice", path=path))
            self.assertEqual(s.resolve("claude_oauth_token"), ("alice-tok", "profile"))
            # cli still beats profile
            s2 = Settings(root=Path(tmp), cli={"claude_oauth_token": "flag"},
                          profile=get_profile("alice", path=path))
            self.assertEqual(s2.resolve("claude_oauth_token")[1], "cli")

    def test_stage_agent_uses_profile_creds(self):
        with TemporaryDirectory() as tmp:
            from clanker.secretstore import get_profile, set_profile
            path = Path(tmp) / "secrets.json"
            set_profile("alice", {"backend": "claude-code", "claude_oauth_token": "alice-tok"}, path=path)
            settings = Settings(root=Path(tmp), profile=get_profile("alice", path=path))
            staging = Path(tmp) / "stage"
            rc = commands.cmd_stage_agent("claude-code", str(staging), settings=settings)
            self.assertEqual(rc, 0)
            env = (staging / "agent/container.env").read_text()
            self.assertIn("CLAUDE_CODE_OAUTH_TOKEN=alice-tok", env)


class SpawnDefaults(unittest.TestCase):
    def test_agent_default_from_env(self):
        with patch.dict(os.environ, {"CTFVM_AGENT": "claude-code"}):
            cmd = UiService._build_start_cmd({"challenge_dir": "/x"})
        self.assertIn("--agent", cmd)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "claude-code")


class Fanout(unittest.TestCase):
    def test_discover_challenges(self):
        from clanker.commands import discover_challenges
        with TemporaryDirectory() as root:
            r = Path(root)
            (r / "alpha").mkdir()
            (r / "alpha" / "description.txt").write_text("first chal\n")
            (r / "alpha" / "ideas.txt").write_text("try uaf\n")
            (r / "beta").mkdir()
            (r / ".hidden").mkdir()          # dotfolder skipped
            (r / "notes.txt").write_text("x")  # file skipped
            found = discover_challenges(root)
            self.assertEqual([c["name"] for c in found], ["alpha", "beta"])
            self.assertEqual(found[0]["description"], "first chal")
            self.assertEqual(found[0]["ideas"], "try uaf")
            self.assertTrue(found[0]["challenge_dir"].endswith("/alpha"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
