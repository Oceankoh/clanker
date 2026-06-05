"""Phase 4: snapshot JSON envelope (B1 marker-collision fix) + challenge state.

    python tests/test_snapshot.py
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker.models import RunRecord  # noqa: E402
from clanker.snapshot import (  # noqa: E402
    build_snapshot_script,
    derive_challenge_state,
    parse_snapshot_response,
)

REC = RunRecord(provider="gcp", run_id="20250101-000000", instance="ctfvm-a-20250101-000000",
                zone="z", project="p", remote_run_dir="/home/ctf/run")


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode("ascii")


def _envelope(**over) -> bytes:
    data = {"error": None, "no_tmux": False, "panes": [], "findings_b64": "",
            "supervisor_b64": "", "explicit_status": None,
            "metrics": {"now": 1000, "findings_mtime": 990, "supervisor_mtime": 995,
                        "artifact_mtime": 0, "artifact_count": 0, "inject_mtime": 0},
            "artifacts": []}
    data.update(over)
    return json.dumps(data).encode("utf-8")


class ScriptShape(unittest.TestCase):
    def test_script_compiles(self):
        src = build_snapshot_script("/home/ctf/run", True)
        compile(src, "<snapshot>", "exec")  # must be valid python
        self.assertIn("RUN_DIR = '/home/ctf/run'", src)
        self.assertIn("INCLUDE_ARTIFACTS = True", src)

    def test_script_runs_and_emits_json(self):
        # Runs locally; without tmux it takes the no_tmux branch, proving the
        # gatherer executes and emits one valid JSON object.
        src = build_snapshot_script("/tmp/does-not-exist", False)
        proc = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)  # single valid JSON object
        self.assertIn("error", payload)
        self.assertIn("panes", payload)


class MarkerCollision(unittest.TestCase):
    def test_pane_and_findings_with_marker_strings_roundtrip(self):
        # The exact content that broke the old marker parser must survive intact.
        nasty_pane = "__PANE_END__ctf|0 __FINDINGS__ a|b|c __SUPERVISOR__ done"
        nasty_findings = "## Notes\n__SUPERVISOR__ __METRICS__ pipe|pipe|pipe\nflag{x}"
        env = _envelope(
            panes=[{"session": "ctf", "index": "0", "name": "supervisor",
                    "active": True, "target": "ctf:supervisor", "output_b64": _b64(nasty_pane)}],
            findings_b64=_b64(nasty_findings),
            supervisor_b64=_b64("supervisor log __WINDOWS_END__ line"),
        )
        snap = parse_snapshot_response(env, record=REC, runtime_status="RUNNING")
        self.assertIsNone(snap.error)
        self.assertEqual(len(snap.panes), 1)
        self.assertEqual(snap.panes[0].output, nasty_pane)          # exact, no truncation
        self.assertEqual(snap.panes[0].window_index, 0)
        self.assertEqual(snap.findings_tail, nasty_findings)        # exact
        self.assertIn("__WINDOWS_END__", snap.supervisor_tail)

    def test_unparseable_is_clean_error(self):
        snap = parse_snapshot_response(b"not json", record=REC, runtime_status="RUNNING")
        self.assertEqual(snap.error, "Unexpected monitor output format.")

    def test_no_tmux_error(self):
        snap = parse_snapshot_response(_envelope(no_tmux=True), record=REC, runtime_status="RUNNING")
        self.assertEqual(snap.error, "No tmux sessions found in VM.")


class ChallengeState(unittest.TestCase):
    def _pane(self):
        from clanker.models import PaneSnapshot
        return PaneSnapshot(session="ctf", window_index=0, window_name="supervisor",
                            active=True, target="ctf:supervisor", output="working...")

    def test_states(self):
        from clanker.models import ExplicitStatus
        panes = [self._pane()]
        base = dict(panes=panes, findings_tail="", supervisor_tail="")

        solved = derive_challenge_state(runtime_status="RUNNING", metrics={},
                                        explicit_status=ExplicitStatus(state="solved"), **base)
        self.assertEqual(solved.state, "solved")

        # the operator's status note must surface in the summary (else it's invisible)
        noted = derive_challenge_state(runtime_status="RUNNING", metrics={},
                                       explicit_status=ExplicitStatus(state="blocked", note="waiting on VPN"), **base)
        self.assertEqual(noted.state, "blocked")
        self.assertIn("waiting on VPN", noted.summary)

        stopped = derive_challenge_state(runtime_status="TERMINATED", metrics={}, explicit_status=None, **base)
        self.assertEqual(stopped.state, "stopped")

        halted = derive_challenge_state(runtime_status="RUNNING", metrics={}, explicit_status=None,
                                        panes=panes, findings_tail="", supervisor_tail="dropping to shell now")
        self.assertEqual(halted.state, "halted")

        no_panes = derive_challenge_state(runtime_status="RUNNING", metrics={}, explicit_status=None,
                                          panes=[], findings_tail="", supervisor_tail="")
        self.assertEqual(no_panes.state, "halted")

        stalled = derive_challenge_state(runtime_status="active", explicit_status=None,
                                         metrics={"now": 10_000, "findings_mtime": 1000}, **base)
        self.assertEqual(stalled.state, "stalled")

        progressing = derive_challenge_state(runtime_status="active", explicit_status=None,
                                             metrics={"now": 1000, "findings_mtime": 990}, **base)
        self.assertEqual(progressing.state, "progressing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
