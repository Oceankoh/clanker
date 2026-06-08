"""Worker runner type: spawn command building, add/remove challenge over the
control plane, grouping, and per-challenge snapshot pane filtering."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker.models import ExecResult  # noqa: E402
from clanker.server.service import ApiError, UiService  # noqa: E402
from clanker.state import RunRegistry  # noqa: E402


def _snap_with_sessions(sessions):
    panes = [{"session": s, "index": "0", "name": "supervisor", "active": True,
              "target": f"{s}:0", "output_b64": base64.b64encode(b"x").decode()} for s in sessions]
    return {"error": None, "no_tmux": False, "panes": panes,
            "findings_b64": "", "supervisor_b64": "", "explicit_status": None,
            "metrics": {"now": 1, "findings_mtime": 0, "supervisor_mtime": 0,
                        "artifact_mtime": 0, "artifact_count": 0, "inject_mtime": 0},
            "artifacts": []}


class FakeClient:
    def __init__(self, sessions=None):
        self.execs = []
        self.uploads = []
        self.tars = []
        self.bodies = {}
        self._sessions = sessions or ["pwn-01"]

    def exec(self, command, *, stdin=b"", timeout=30):
        self.execs.append(command)
        if "python3 -" in command:  # snapshot fetch
            return ExecResult(0, stdout=json.dumps(_snap_with_sessions(self._sessions)).encode())
        return ExecResult(0)

    def upload_file(self, remote_path, body, *, mode="", timeout=60):
        self.uploads.append((remote_path, len(body), mode))
        self.bodies[remote_path] = body

    def upload_tar(self, remote_dir, body, *, timeout=120):
        self.tars.append((remote_dir, len(body)))


class _Job:
    def __init__(self, state="running", output=""):
        self.state = state
        self.output = output
        self.job_id = "j"
        self.started_at = ""
        self.finished_at = ""


class _FakeJobs:
    def __init__(self):
        self.submitted = []
        self.by_run = {}  # run_id -> [_Job, ...]

    def submit(self, cmd):
        self.submitted.append(cmd)
        class J:  # noqa: E306
            job_id = f"job-{len(self.submitted)}"
        return J()

    def for_run(self, run_id):
        return self.by_run.get(run_id, [])

    def active_count(self):
        return 0


def _service(tmp: Path, *, client=None):
    runs = tmp / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20250101-000000.json").write_text(json.dumps({
        "provider": "digitalocean", "run_id": "20250101-000000",
        "instance": "ctfvm-worker-20250101-000000", "zone": "nyc3", "project": "digitalocean",
        "ip": "5.6.7.8", "control_host": "5.6.7.8", "control_port": "443",
        "control_user": "u", "control_password": "pw", "runner_type": "worker",
        "challenge_name": "worker-01",
    }))
    registry = RunRegistry(runs_dir=runs, state_file=tmp / "current.json", status=lambda r: "active")
    fake = client or FakeClient()
    svc = UiService(registry=registry, providers=object(), jobs=_FakeJobs(),
                    client_factory=lambda rec: fake)
    return svc, fake, registry


class WorkerStartCommandTest(unittest.TestCase):
    def test_count_and_names(self):
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            cmds = svc.worker_start_commands({"count": 3, "provider": "digitalocean"})
            self.assertEqual(len(cmds), 3)
            for c in cmds:
                self.assertIn("--worker", c)
                self.assertIn("start", c)
            names = [c[c.index("--name") + 1] for c in cmds]
            self.assertEqual(names, ["worker-02", "worker-03", "worker-04"])  # worker-01 exists

    def test_spawn_requires_count(self):
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            with self.assertRaises(ApiError):
                svc.spawn_workers({"provider": "digitalocean"})


class AddRemoveChallengeTest(unittest.TestCase):
    def _patch_stage(self, svc):
        # skip real credential materialization in unit tests
        svc._stage_agent_remote = lambda client, ws, backend, payload: client.upload_file(
            f"{ws}/agent/backend", (backend + "\n").encode())

    def test_add_challenge(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))
            self._patch_stage(svc)
            res = svc.add_challenge("20250101-000000", {"name": "Pwn 01", "agent_backend": "codex",
                                                        "description": "own it", "ideas": "try ret2win",
                                                        "flag_format": "flag{...}"}, archive=b"TAR")
            self.assertEqual(res["slug"], "pwn-01")
            self.assertEqual(res["tmux_session"], "pwn-01:supervisor")
            self.assertEqual(res["workspace"], "/home/ctf/run/pwn-01")
            # uploaded the challenge tar + prompt
            self.assertTrue(any("/home/ctf/run/pwn-01/challenge" == t[0] for t in fake.tars))
            # prompt has the SAME shape as a normal run (desc + ideas + instructions + flag fmt)
            prompt = fake.bodies["/home/ctf/run/pwn-01/challenge_prompt.txt"].decode()
            self.assertIn("Challenge description:", prompt)
            self.assertIn("own it", prompt)
            self.assertIn("Initial ideas:", prompt)
            self.assertIn("try ret2win", prompt)
            self.assertIn("Expected flag format: flag{...}", prompt)
            self.assertIn("/home/ctf/run/pwn-01/challenge", prompt)   # /workspace remapped
            # shipped prompts/ + skills/ like a normal run
            self.assertTrue(any(t[0] == "/home/ctf/run/pwn-01/prompts" for t in fake.tars))
            self.assertTrue(any(t[0] == "/home/ctf/run/pwn-01/.codex/skills" for t in fake.tars))
            # launched a per-slug tmux session
            self.assertTrue(any("tmux new-session -d -s pwn-01" in c for c in fake.execs))
            # registered a challenge record sharing the worker's endpoint, ip blank
            rec = registry.resolve(res["run_id"])
            self.assertEqual(rec.parent_worker_id, "20250101-000000")
            self.assertEqual(rec.tmux_session, "pwn-01:supervisor")
            self.assertEqual(rec.control_host, "5.6.7.8")
            self.assertEqual(rec.ip, "")

    def test_add_challenge_unique_slug(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))
            self._patch_stage(svc)
            a = svc.add_challenge("20250101-000000", {"name": "pwn"}, archive=b"T")
            b = svc.add_challenge("20250101-000000", {"name": "pwn"}, archive=b"T")
            self.assertEqual(a["slug"], "pwn")
            self.assertEqual(b["slug"], "pwn-2")

    def test_add_to_non_worker_rejected(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))
            self._patch_stage(svc)
            # make a plain challenge record
            (Path(d) / "runs" / "20250102-000000.json").write_text(json.dumps({
                "provider": "gcp", "run_id": "20250102-000000", "instance": "ctfvm-a-20250102-000000",
                "zone": "z", "project": "p", "control_host": "1.1.1.1", "control_port": "443",
                "control_user": "u", "control_password": "pw", "runner_type": "challenge"}))
            with self.assertRaises(ApiError):
                svc.add_challenge("20250102-000000", {"name": "x"}, archive=b"T")

    def test_remove_challenge(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))
            self._patch_stage(svc)
            res = svc.add_challenge("20250101-000000", {"name": "web-1"}, archive=b"T")
            out = svc.remove_challenge("20250101-000000", "web-1")
            self.assertEqual(out["removed_run_id"], res["run_id"])
            self.assertIsNone(registry.resolve(res["run_id"]))
            self.assertTrue(any("kill-session -t web-1" in c for c in fake.execs))


class ListWorkersTest(unittest.TestCase):
    def test_grouping(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))
            svc._stage_agent_remote = lambda *a, **k: None
            svc.add_challenge("20250101-000000", {"name": "pwn-01"}, archive=b"T")
            groups = svc.list_workers()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["worker"].record.challenge_name, "worker-01")
            self.assertEqual(len(groups[0]["challenges"]), 1)
            self.assertEqual(groups[0]["challenges"][0].record.parent_worker_id, "20250101-000000")


class ChallengeRuntimeStatusTest(unittest.TestCase):
    def test_challenge_inherits_worker_status(self):
        with TemporaryDirectory() as d:
            svc, fake, registry = _service(Path(d))   # worker status fn returns "active"
            svc._stage_agent_remote = lambda *a, **k: None
            svc.add_challenge("20250101-000000", {"name": "pwn-01"}, archive=b"T")
            listings, _ = registry.list_runs(include_status=True)
            ch = next(l for l in listings if l.record.parent_worker_id == "20250101-000000")
            self.assertEqual(ch.runtime_status, "active")    # not UNKNOWN
            self.assertTrue(ch.is_runtime_active)


class ChallengeSnapshotFilterTest(unittest.TestCase):
    def test_panes_filtered_to_own_session(self):
        with TemporaryDirectory() as d:
            client = FakeClient(sessions=["pwn-01", "web-02", "subagent-pwn-01-abc", "subagent-web-02-xyz"])
            svc, fake, registry = _service(Path(d), client=client)
            svc._stage_agent_remote = lambda *a, **k: None
            res = svc.add_challenge("20250101-000000", {"name": "pwn-01"}, archive=b"T")
            snap = svc.snapshot(res["run_id"])
            seen = {p.session for p in snap.panes}
            self.assertEqual(seen, {"pwn-01", "subagent-pwn-01-abc"})


class WorkerStateTest(unittest.TestCase):
    WORKER = "20250101-000000"

    def test_reachable_worker_is_ready_not_halted(self):
        # A worker runs no agent, so the agent-centric derive would call it
        # "Halted" (no panes). With its control plane reachable and no spawn job
        # running, it must read as "ready".
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            snap = svc.snapshot(self.WORKER)
            self.assertEqual(snap.challenge_state.state, "ready")

    def test_worker_provisioning_while_spawn_job_runs(self):
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            svc.jobs.by_run[self.WORKER] = [_Job(state="running")]
            snap = svc.snapshot(self.WORKER)
            self.assertEqual(snap.challenge_state.state, "provisioning")

    def test_worker_failed_when_spawn_job_errored(self):
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            svc.jobs.by_run[self.WORKER] = [_Job(state="error", output="image pull 404")]
            snap = svc.snapshot(self.WORKER)
            self.assertEqual(snap.challenge_state.state, "failed")


class WorkerChallengeVpnStatusTest(unittest.TestCase):
    WORKER = "20250101-000000"

    def _vpn_status_with_root(self, svc, root: Path, run_id: str):
        import clanker.server.service as service_mod
        orig = service_mod.ROOT
        service_mod.ROOT = root
        try:
            return svc.vpn_status(run_id)
        finally:
            service_mod.ROOT = orig

    def test_challenge_reports_worker_tunnel_connected(self):
        # The worker's tunnel is up (state file keyed by the WORKER run_id); a
        # hosted challenge shares it, so its vpn_status must read "connected".
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            svc._stage_agent_remote = lambda *a, **k: None
            child = svc.add_challenge(self.WORKER, {"name": "pwn-01"}, archive=b"T")["run_id"]
            vpn_sub = Path(d) / ".ctfvm" / "vpn" / "sess"
            vpn_sub.mkdir(parents=True)
            (vpn_sub / "iface.json").write_text(json.dumps({
                "run_id": self.WORKER, "local_interface": "ctfvmX",
                "local_ip": "10.88.0.1", "remote_ip": "10.88.0.2"}))
            st = self._vpn_status_with_root(svc, Path(d), child)
            self.assertTrue(st["connected"])
            self.assertIn(self.WORKER, st["up_command"])      # points at the worker
            self.assertNotIn(child, st["up_command"])         # not the challenge

    def test_challenge_down_points_bringup_at_worker(self):
        with TemporaryDirectory() as d:
            svc, _, _ = _service(Path(d))
            svc._stage_agent_remote = lambda *a, **k: None
            child = svc.add_challenge(self.WORKER, {"name": "pwn-01"}, archive=b"T")["run_id"]
            st = self._vpn_status_with_root(svc, Path(d), child)  # no tunnel up
            self.assertFalse(st["connected"])
            self.assertIn(self.WORKER, st["up_command"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
