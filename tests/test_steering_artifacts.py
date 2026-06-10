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

    def test_status_writes_to_run_dir(self):
        # Worker-hosted challenges share a VM but each has its own run dir; the
        # status file must land under that dir (matching where the snapshot
        # gatherer reads it), not the hardcoded /home/ctf/run parent.
        c = FakeClient()
        steering.set_explicit_status(c, "solved", run_dir="/home/ctf/run/web-1")
        self.assertIn("/home/ctf/run/web-1/ui-status.json", c.commands[0])
        steering.clear_explicit_status(c, "/home/ctf/run/web-1")
        self.assertIn("/home/ctf/run/web-1/ui-status.json", c.commands[1])

    def test_status_default_run_dir_unchanged(self):
        # Normal challenges (no per-slug dir) keep the legacy path.
        c = FakeClient()
        steering.set_explicit_status(c, "blocked")
        self.assertIn("/home/ctf/run/ui-status.json", c.commands[0])

    def test_status_rejects_run_dir_with_quote(self):
        c = FakeClient()
        with self.assertRaises(SteeringError):
            steering.set_explicit_status(c, "solved", run_dir="/home/ctf/run/x'y")
        self.assertEqual(c.commands, [])

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

    def test_download_reads_as_ctf_with_realpath_guard(self):
        body = json.dumps({"ok": True, "mime": "text/x-python",
                           "b64": base64.b64encode(b"payload").decode()})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        dl = artifacts.download_artifact(c, "/home/ctf/run", "artifacts/exploit.py")
        self.assertEqual(dl.content, b"payload")
        self.assertEqual(dl.filename, "exploit.py")
        # never uses the root-reading /files/download for artifacts
        self.assertEqual(c.downloads, [])
        # the remote program is run as ctf and carries the artifacts-dir guard
        self.assertIn("sudo -u ctf", c.commands[0])
        self.assertIn("base64 -d", c.commands[0])

    def test_download_not_found(self):
        body = json.dumps({"ok": False})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        with self.assertRaises(ArtifactError):
            artifacts.download_artifact(c, "/home/ctf/run", "artifacts/missing")

    def test_bundle_cds_into_run_dir(self):
        # Worker-hosted challenges live under /home/ctf/run/<slug>; the bundle
        # must cd there, else it tars the shared parent (sibling challenges).
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=b"/tmp/ctfvm-bundle-1.tar.gz"),
                       download=b"tarbytes")
        dl = artifacts.build_bundle(c, "run-x", "/home/ctf/run/web-1")
        self.assertEqual(dl.content, b"tarbytes")
        self.assertIn("cd /home/ctf/run/web-1 ", c.commands[0])
        self.assertNotIn("cd /home/ctf/run ", c.commands[0])

    def test_bundle_default_run_dir_unchanged(self):
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=b"/tmp/ctfvm-bundle-1.tar.gz"),
                       download=b"tarbytes")
        artifacts.build_bundle(c, "run-x")
        self.assertIn("cd /home/ctf/run ", c.commands[0])

    def test_bundle_rejects_run_dir_with_quote(self):
        c = FakeClient()
        with self.assertRaises(ArtifactError):
            artifacts.build_bundle(c, "run-x", "/home/ctf/run/x'y")
        self.assertEqual(c.commands, [])

    def test_preview_parses_json_envelope(self):
        body = json.dumps({"ok": True, "mime": "text/x-python", "size": 5,
                           "truncated": False, "b64": base64.b64encode(b"print").decode()})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        pv = artifacts.preview_artifact(c, "/home/ctf/run", "artifacts/exploit.py")
        self.assertTrue(pv.is_text)
        self.assertEqual(pv.content, "print")
        self.assertEqual(pv.relpath, "artifacts/exploit.py")

    def test_preview_binary_exposes_b64_not_text(self):
        blob = b"\x00\x01\x02\x03ELF"
        body = json.dumps({"ok": True, "mime": "application/octet-stream", "size": len(blob),
                           "truncated": False, "b64": base64.b64encode(blob).decode()})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        pv = artifacts.preview_artifact(c, "/home/ctf/run", "artifacts/chal")
        self.assertFalse(pv.is_text)
        self.assertEqual(pv.b64, base64.b64encode(blob).decode())  # bytes for hex view

    def test_preview_not_found(self):
        body = json.dumps({"ok": False})
        c = FakeClient(exec_result=ExecResult(returncode=0, stdout=body.encode()))
        with self.assertRaises(ArtifactError):
            artifacts.preview_artifact(c, "/home/ctf/run", "artifacts/missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
