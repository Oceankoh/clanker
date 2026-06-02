"""Phase 4: steering + artifacts (control-plane only, B7; validation R1/R2).

    python tests/test_steering_artifacts.py
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker import artifacts, steering  # noqa: E402
from clanker.artifacts import ArtifactError  # noqa: E402
from clanker.controlclient import ControlPlaneError  # noqa: E402
from clanker.models import ExecResult  # noqa: E402
from clanker.steering import SteeringError  # noqa: E402
from clanker.validation import sanitize_relpath  # noqa: E402


class FakeClient:
    """Records exec commands and returns scripted results; no network."""

    def __init__(self, exec_result=None, download=b""):
        self.commands = []
        self.downloads = []
        self._exec_result = exec_result or ExecResult(returncode=0)
        self._download = download

    def exec(self, command, *, stdin=b"", timeout=30):
        self.commands.append(command)
        return self._exec_result

    def download_file(self, remote_path, *, timeout=60):
        self.downloads.append(remote_path)
        return self._download


class Validation(unittest.TestCase):
    def test_sanitize_relpath(self):
        self.assertEqual(sanitize_relpath("artifacts/exploit.py"), "artifacts/exploit.py")
        self.assertEqual(sanitize_relpath("artifacts/./a/b.txt"), "artifacts/a/b.txt")
        self.assertIsNone(sanitize_relpath("artifacts/../../etc/passwd"))
        self.assertIsNone(sanitize_relpath("/etc/passwd"))
        self.assertIsNone(sanitize_relpath("~/.ssh/id_rsa"))
        self.assertIsNone(sanitize_relpath("logs/supervisor.log"))  # outside artifacts/


class Steering(unittest.TestCase):
    def test_invalid_target_rejected_without_remote_call(self):
        c = FakeClient()
        with self.assertRaises(SteeringError):
            steering.send_text(c, "ctf:supervisor;rm -rf /", "hi")
        self.assertEqual(c.commands, [])  # VM never contacted

    def test_special_chars_are_base64_not_raw(self):
        c = FakeClient()
        text = "note: a|b `c` $HOME ; rm -rf /"
        steering.send_text(c, "ctf:supervisor", text, enter=True)
        self.assertEqual(len(c.commands), 1)
        cmd = c.commands[0]
        # the raw dangerous text must NOT appear; only its base64 does
        self.assertNotIn("rm -rf /", cmd)
        self.assertIn(base64.b64encode(text.encode()).decode(), cmd)
        self.assertIn("C-m", cmd)  # enter

    def test_send_keys_validation(self):
        c = FakeClient()
        with self.assertRaises(SteeringError):
            steering.send_keys(c, "ctf:supervisor", ["; rm -rf /"])
        self.assertEqual(c.commands, [])
        steering.send_keys(c, "ctf:supervisor", ["C-c"])
        self.assertIn("send-keys", c.commands[0])

    def test_set_status_validation(self):
        c = FakeClient()
        with self.assertRaises(SteeringError):
            steering.set_explicit_status(c, "bogus")
        steering.set_explicit_status(c, "solved", note="flag{x}")
        self.assertIn("ui-status.json", c.commands[0])

    def test_remote_failure_raises(self):
        c = FakeClient(exec_result=ExecResult(returncode=1, stderr=b"no session"))
        with self.assertRaises(ControlPlaneError):
            steering.send_text(c, "ctf:supervisor", "hi")


class Artifacts(unittest.TestCase):
    def test_download_rejects_traversal_without_remote_call(self):
        c = FakeClient()
        with self.assertRaises(ArtifactError):
            artifacts.download_artifact(c, "/home/ctf/run", "artifacts/../../etc/passwd")
        self.assertEqual(c.downloads, [])

    def test_download_uses_abspath(self):
        c = FakeClient(download=b"payload")
        dl = artifacts.download_artifact(c, "/home/ctf/run", "artifacts/exploit.py")
        self.assertEqual(c.downloads, ["/home/ctf/run/artifacts/exploit.py"])
        self.assertEqual(dl.content, b"payload")
        self.assertEqual(dl.filename, "exploit.py")

    def test_preview_parses_json_envelope(self):
        body = json.dumps({"ok": True, "mime": "text/x-python", "size": 5,
                           "truncated": False, "b64": base64.b64encode(b"print").decode()})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        pv = artifacts.preview_artifact(c, "/home/ctf/run", "artifacts/exploit.py")
        self.assertTrue(pv.is_text)
        self.assertEqual(pv.content, "print")
        self.assertEqual(pv.relpath, "artifacts/exploit.py")

    def test_preview_not_found(self):
        body = json.dumps({"ok": False})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        with self.assertRaises(ArtifactError):
            artifacts.preview_artifact(c, "/home/ctf/run", "artifacts/missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
