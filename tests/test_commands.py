"""Phase 2: ported read-only commands + provider layer.

    python tests/test_commands.py
    python -m pytest tests/test_commands.py
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker import commands  # noqa: E402
from clanker.models import RunRecord  # noqa: E402
from clanker.providers.base import CloudProvider, CloudProviderRegistry  # noqa: E402
from clanker.state import RunRegistry  # noqa: E402


class FakeProvider(CloudProvider):
    def __init__(self, name, statuses, cli=True):
        self.name = name
        self._statuses = statuses
        self._cli = cli

    @property
    def cli_available(self):
        return self._cli

    def discover_runs(self):
        return []

    def get_status(self, run):
        return self._statuses.get(run.instance, "UNKNOWN")


def _registry(statuses, cli=True):
    return CloudProviderRegistry([FakeProvider("gcp", statuses, cli=cli)])


def _write(path, **fields):
    base = {"provider": "gcp", "zone": "z", "project": "p"}
    base.update(fields)
    path.write_text(json.dumps(base))


class CleanupState(unittest.TestCase):
    def _setup(self, tmp):
        runs = Path(tmp) / "runs"
        runs.mkdir(parents=True)
        state = Path(tmp) / "current-run.json"
        # patch the module-level paths the command reads
        commands.RUNS_DIR = runs
        commands.STATE_FILE = state
        return runs, state

    def test_removes_gone_and_incomplete_keeps_running_and_promotes(self):
        with TemporaryDirectory() as tmp:
            runs, state = self._setup(tmp)
            _write(runs / "20250101-000000.json", instance="ctfvm-run1-20250101-000000")  # RUNNING
            _write(runs / "20250102-000000.json", instance="ctfvm-gone-20250102-000000")  # gone
            _write(runs / "bad.json", instance="ctfvm-bad", zone="")                       # incomplete

            statuses = {"ctfvm-run1-20250101-000000": "RUNNING", "ctfvm-gone-20250102-000000": ""}
            rc = commands.cmd_cleanup_state(_registry(statuses))
            self.assertEqual(rc, 0)
            self.assertTrue((runs / "20250101-000000.json").exists())
            self.assertFalse((runs / "20250102-000000.json").exists())
            self.assertFalse((runs / "bad.json").exists())
            # current-run.json was missing -> promoted from the surviving run
            self.assertTrue(state.exists())

    def test_prune_non_running_removes_stopped(self):
        with TemporaryDirectory() as tmp:
            runs, state = self._setup(tmp)
            _write(runs / "r1.json", instance="ctfvm-run1-20250101-000000")
            _write(runs / "r2.json", instance="ctfvm-stop-20250102-000000")
            statuses = {"ctfvm-run1-20250101-000000": "RUNNING", "ctfvm-stop-20250102-000000": "TERMINATED"}

            commands.cmd_cleanup_state(_registry(statuses), prune_non_running=True)
            self.assertTrue((runs / "r1.json").exists())
            self.assertFalse((runs / "r2.json").exists())

    def test_cli_unavailable_keeps_everything(self):
        with TemporaryDirectory() as tmp:
            runs, state = self._setup(tmp)
            _write(runs / "r1.json", instance="ctfvm-run1-20250101-000000")
            # provider CLI missing -> never removed even though status would be ''
            commands.cmd_cleanup_state(_registry({}, cli=False), prune_non_running=True)
            self.assertTrue((runs / "r1.json").exists())

    def test_dry_run_changes_nothing(self):
        with TemporaryDirectory() as tmp:
            runs, state = self._setup(tmp)
            _write(runs / "20250102-000000.json", instance="ctfvm-gone-20250102-000000")
            commands.cmd_cleanup_state(_registry({"ctfvm-gone-20250102-000000": ""}), dry_run=True)
            self.assertTrue((runs / "20250102-000000.json").exists())
            self.assertFalse(state.exists())


class Status(unittest.TestCase):
    def test_prints_core_fields(self):
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir(parents=True)
            state_file = Path(tmp) / "current-run.json"
            commands.RUNS_DIR = runs
            commands.STATE_FILE = state_file
            _write(
                runs / "20250101-000000.json",
                run_id="20250101-000000",
                instance="ctfvm-a-20250101-000000",
                ip="1.2.3.4",
                control_host="1.2.3.4", control_port="443",
                control_user="u", control_password="pw",
                toolbox_image_ref="reg/ctf-toolbox:lean",
            )
            registry = RunRegistry(runs_dir=runs, state_file=state_file)
            providers = _registry({"ctfvm-a-20250101-000000": "RUNNING"})

            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = commands.cmd_status(registry, providers, run_id="20250101-000000")
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("Provider: GCP", out)
            self.assertIn("Status:   RUNNING", out)
            self.assertIn("IP:       1.2.3.4", out)
            self.assertIn("Control:  http://1.2.3.4:443", out)
            self.assertIn("Toolbox:  reg/ctf-toolbox:lean", out)
            self.assertIn("Run ID:   20250101-000000", out)

    def test_missing_run_returns_1(self):
        with TemporaryDirectory() as tmp:
            registry = RunRegistry(runs_dir=Path(tmp) / "runs", state_file=Path(tmp) / "none.json")
            rc = commands.cmd_status(registry, _registry({}), run_id="nope")
            self.assertEqual(rc, 1)

    def test_empty_run_id_does_not_leak_current_run_extras(self):
        # An IP-only run (no run_id) must NOT pick up the current run's extras.
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir(parents=True)
            state_file = Path(tmp) / "current-run.json"
            commands.RUNS_DIR = runs
            commands.STATE_FILE = state_file
            # current run is a DIFFERENT gcp run carrying a distinctive toolbox ref
            _write(
                state_file, run_id="20250101-000000",
                instance="ctfvm-gcp-20250101-000000", toolbox_image_ref="GCPTOOLBOX",
            )
            # target: a DO run with NO run_id (instance has no date suffix)
            (runs / "do.json").write_text(json.dumps({
                "provider": "digitalocean", "instance": "ctfvm-do-x",
                "zone": "nyc3", "project": "digitalocean", "ip": "5.6.7.8",
            }))
            registry = RunRegistry(runs_dir=runs, state_file=state_file)
            providers = CloudProviderRegistry([
                FakeProvider("gcp", {}), FakeProvider("digitalocean", {"ctfvm-do-x": "active"}),
            ])

            buf = io.StringIO()
            with redirect_stdout(buf):
                commands.cmd_status(registry, providers, instance="ctfvm-do-x")
            out = buf.getvalue()
            self.assertIn("Provider: DigitalOcean", out)
            self.assertNotIn("GCPTOOLBOX", out)  # must not bleed from current-run.json


class _FakeClient:
    def __init__(self, exec_rc=0):
        from clanker.models import ExecResult
        self._rc = exec_rc
        self.downloaded = False
        self._ExecResult = ExecResult

    def exec(self, command, *, stdin=b"", timeout=30):
        return self._ExecResult(returncode=self._rc, stderr=b"boom" if self._rc else b"")

    def download_file(self, remote_path, *, timeout=60):
        self.downloaded = True
        return b""


class FetchSyncDown(unittest.TestCase):
    def _run_with_fake(self, fn_name, exec_rc):
        with TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir(parents=True)
            (runs / "20250101-000000.json").write_text(json.dumps({
                "provider": "gcp", "run_id": "20250101-000000",
                "instance": "ctfvm-a-20250101-000000", "zone": "z", "project": "p",
                "control_host": "1.2.3.4", "control_port": "443",
                "control_user": "u", "control_password": "pw",
            }))
            registry = RunRegistry(runs_dir=runs, state_file=Path(tmp) / "none.json")
            fake = _FakeClient(exec_rc=exec_rc)

            class FakeCPC:
                @staticmethod
                def from_run(record, **kw):
                    return fake

            orig = commands.ControlPlaneClient
            commands.ControlPlaneClient = FakeCPC
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    fn = getattr(commands, fn_name)
                    rc = fn(registry, run_id="20250101-000000", out_dir=str(Path(tmp) / "out"))
            finally:
                commands.ControlPlaneClient = orig
            return rc, fake

    def test_fetch_aborts_when_remote_archive_fails(self):
        rc, fake = self._run_with_fake("cmd_fetch", exec_rc=1)
        self.assertEqual(rc, 1)
        self.assertFalse(fake.downloaded, "must not download after a failed archive step")


class ProviderRegistryWiring(unittest.TestCase):
    def test_get_normalizes_and_raises(self):
        reg = CloudProviderRegistry([FakeProvider("gcp", {}), FakeProvider("digitalocean", {})])
        self.assertEqual(reg.get("do").name, "digitalocean")   # alias normalizes
        self.assertEqual(reg.get("google").name, "gcp")
        with self.assertRaises(KeyError):
            reg.get("aws")

    def test_get_status_dispatches_by_provider(self):
        reg = CloudProviderRegistry([
            FakeProvider("gcp", {"ctfvm-g": "RUNNING"}),
            FakeProvider("digitalocean", {"ctfvm-d": "active"}),
        ])
        g = RunRecord(provider="gcp", run_id="", instance="ctfvm-g", zone="z", project="p")
        d = RunRecord(provider="digitalocean", run_id="", instance="ctfvm-d", zone="nyc3", project="digitalocean")
        self.assertEqual(reg.get_status(g), "RUNNING")
        self.assertEqual(reg.get_status(d), "active")


class ConfineRemotePathTest(unittest.TestCase):
    RUN_DIR = "/home/ctf/run"

    def test_relative_joins_workspace(self):
        self.assertEqual(commands.confine_remote_path(self.RUN_DIR, "a/b.txt"), "/home/ctf/run/a/b.txt")

    def test_workspace_root_itself_allowed(self):
        self.assertEqual(commands.confine_remote_path(self.RUN_DIR, "."), self.RUN_DIR)

    def test_absolute_inside_workspace_allowed(self):
        self.assertEqual(commands.confine_remote_path(self.RUN_DIR, "/home/ctf/run/x"), "/home/ctf/run/x")

    def test_relative_traversal_escape_rejected(self):
        with self.assertRaises(ValueError):
            commands.confine_remote_path(self.RUN_DIR, "../../etc/passwd")

    def test_absolute_outside_rejected_without_optin(self):
        with self.assertRaises(ValueError):
            commands.confine_remote_path(self.RUN_DIR, "/etc/passwd")

    def test_absolute_outside_allowed_with_optin(self):
        self.assertEqual(commands.confine_remote_path(self.RUN_DIR, "/etc/passwd", allow_abs=True), "/etc/passwd")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            commands.confine_remote_path(self.RUN_DIR, "  ")


class BuildChallengePromptTest(unittest.TestCase):
    def test_includes_desc_ideas_and_instructions(self):
        p = commands.build_challenge_prompt("a heap UAF", "try tcache poisoning")
        self.assertIn("Challenge description:", p)
        self.assertIn("a heap UAF", p)
        self.assertIn("Initial ideas:", p)
        self.assertIn("try tcache poisoning", p)
        self.assertIn("findings.md", p)   # from instructions.txt (or the fallback)

    def test_blank_fields_say_none_provided(self):
        p = commands.build_challenge_prompt("", "")
        self.assertIn("Challenge description:\n(none provided)", p)
        self.assertIn("Initial ideas:\n(none provided)", p)

    def test_missing_instructions_uses_fallback(self):
        with TemporaryDirectory() as d:
            p = commands.build_challenge_prompt("x", "y", root=Path(d))
            self.assertIn("Objectives:", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
