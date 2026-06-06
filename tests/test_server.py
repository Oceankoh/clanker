"""Phase 5: HTTP server / /api/v1 surface (in-process, fake control client)."""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from clanker.models import ExecResult  # noqa: E402
from clanker.server.app import App, make_handler  # noqa: E402
from clanker.server.jobs import SpawnJobTracker  # noqa: E402
from clanker.server.service import UiService  # noqa: E402
from clanker.state import RunRegistry  # noqa: E402

_SNAP = {
    "error": None, "no_tmux": False,
    "panes": [
        {"session": "ctf", "index": "0", "name": "supervisor", "active": True,
         "target": "ctf:supervisor", "output_b64": base64.b64encode(b"working __FINDINGS__ a|b").decode()},
        {"session": "subagent-abc", "index": "0", "name": "exploit", "active": False,
         "target": "subagent-abc:0", "output_b64": base64.b64encode(b"sub work").decode()},
    ],
    "findings_b64": base64.b64encode(b"## Notes\nflag{x}").decode(),
    "supervisor_b64": base64.b64encode(b"log line").decode(),
    "explicit_status": None,
    "metrics": {"now": 1000, "findings_mtime": 990, "supervisor_mtime": 995,
                "artifact_mtime": 0, "artifact_count": 1, "inject_mtime": 0},
    "artifacts": [{"relpath": "artifacts/exploit.py", "size": 5, "mtime": 990}],
}
_ART = {"ok": True, "mime": "text/x-python", "size": 5, "truncated": False,
        "b64": base64.b64encode(b"print").decode()}


class FakeClient:
    def __init__(self):
        self.commands = []

    def exec(self, command, *, stdin=b"", timeout=30):
        self.commands.append(command)
        if "python3 -" in command:
            m = re.search(r"printf %s (\S+) \|", command)
            src = base64.b64decode(m.group(1)).decode() if m else ""
            if "ART_DIR" in src:
                return ExecResult(0, stdout=json.dumps(_ART).encode())
            if "PATTERN" in src:  # transcript fetch
                lines = "\n".join(json.dumps(o) for o in [
                    {"type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}]}},
                    {"type": "response_item", "payload": {"type": "function_call",
                        "name": "exec_command", "call_id": "c", "arguments": "{}"}},
                ])
                env = {"file": "rollout-x.jsonl", "b64": base64.b64encode(lines.encode()).decode()}
                return ExecResult(0, stdout=json.dumps(env).encode())
            return ExecResult(0, stdout=json.dumps(_SNAP).encode())
        if "tar -czf" in command:
            return ExecResult(0, stdout=b"/tmp/ctfvm-bundle-1.tar.gz\n")
        return ExecResult(0)

    def download_file(self, remote_path, *, timeout=60):
        return b"BUNDLE"


def _make_service(tmp: Path) -> UiService:
    runs = tmp / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "20250101-000000.json").write_text(json.dumps({
        "provider": "gcp", "run_id": "20250101-000000", "instance": "ctfvm-a-20250101-000000",
        "zone": "z", "project": "p", "ip": "1.2.3.4",
        "control_host": "1.2.3.4", "control_port": "443", "control_user": "u", "control_password": "pw",
    }))
    registry = RunRegistry(runs_dir=runs, state_file=tmp / "current.json", status=lambda r: "RUNNING")
    return UiService(registry=registry, providers=object(), jobs=_FakeTracker(now=lambda: "t"),
                     client_factory=lambda rec: FakeClient())


class _FakeTracker(SpawnJobTracker):
    """Never runs the real ctfvm start — just records + marks done."""
    def _run_subprocess(self, job, command):
        job.state = "done"
        job.run_id = "20260101-000000"
        job.finished_at = self._now()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = TemporaryDirectory()
        service = _make_service(Path(cls._tmpdir.name))
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(service)))
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls._tmpdir.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, r.read(), r.headers

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    # --- tests -------------------------------------------------------------
    def test_health(self):
        st, body, _ = self._get("/health")
        j = json.loads(body)
        self.assertTrue(j["ok"])
        self.assertEqual(j["data"]["known_runs"], 1)

    def test_runs_list(self):
        st, body, _ = self._get("/api/v1/runs?include_status=true")
        j = json.loads(body)["data"]
        self.assertEqual(len(j["runs"]), 1)
        self.assertEqual(j["runs"][0]["runtime_status"], "RUNNING")
        self.assertEqual(j["runs"][0]["agent_backend"], "codex")

    def test_snapshot_and_subagents(self):
        st, body, _ = self._get("/api/v1/runs/20250101-000000")
        d = json.loads(body)["data"]
        self.assertIsNone(d["error"])
        # marker content survived (B1) and pane present
        self.assertIn("__FINDINGS__", d["panes"][0]["output"])
        self.assertEqual(len(d["subagents"]), 1)
        self.assertEqual(d["subagents"][0]["tmux_target"], "subagent-abc:0")
        self.assertEqual(d["challenge_state"]["state"], "progressing")

    def test_steering_bad_target_400(self):
        st, j = self._req("POST", "/api/v1/runs/20250101-000000/panes/send",
                          {"target": "ctf:supervisor;rm -rf /", "text": "x"})
        self.assertEqual(st, 400)
        self.assertEqual(j["code"], "BAD_REQUEST")

    def test_steering_ok(self):
        st, j = self._req("POST", "/api/v1/runs/20250101-000000/panes/send",
                          {"target": "ctf:supervisor", "text": "a|b `c` $X"})
        self.assertEqual(st, 200)
        self.assertTrue(j["ok"])

    def test_transcript(self):
        st, body, _ = self._get("/api/v1/runs/20250101-000000/transcript")
        d = json.loads(body)["data"]
        self.assertEqual(d["backend"], "codex")
        kinds = [e["kind"] for e in d["events"]]
        self.assertIn("message", kinds)
        self.assertIn("tool_call", kinds)
        self.assertEqual(next(e for e in d["events"] if e["kind"] == "tool_call")["tool"], "exec_command")

    def test_inject_route_removed(self):
        # the dead inject.queue endpoint was removed (no VM consumer; CLIs queue natively)
        st, j = self._req("POST", "/api/v1/runs/20250101-000000/inject", {"text": "hint"})
        self.assertEqual(st, 404)

    def test_status_set_clear(self):
        st, j = self._req("POST", "/api/v1/runs/20250101-000000/status", {"state": "solved"})
        self.assertEqual(st, 200)
        st, j = self._req("DELETE", "/api/v1/runs/20250101-000000/status")
        self.assertEqual(st, 200)

    def test_artifact_preview_and_download(self):
        st, body, _ = self._get("/api/v1/runs/20250101-000000/artifacts/artifacts%2Fexploit.py")
        d = json.loads(body)["data"]
        self.assertTrue(d["is_text"])
        self.assertEqual(d["content"], "print")
        st, body, hdr = self._get("/api/v1/runs/20250101-000000/artifacts/artifacts%2Fexploit.py/download")
        self.assertEqual(body, b"print")
        self.assertIn("attachment", hdr.get("Content-Disposition", ""))

    def test_bundle(self):
        st, body, hdr = self._get("/api/v1/runs/20250101-000000/bundle")
        self.assertEqual(body, b"BUNDLE")

    def test_agents_endpoint(self):
        st, body, _ = self._get("/api/v1/agents")
        names = [a["name"] for a in json.loads(body)["data"]["agents"]]
        self.assertIn("codex", names)
        self.assertIn("claude-code", names)

    def test_profiles_endpoint(self):
        st, body, _ = self._get("/api/v1/profiles")
        d = json.loads(body)["data"]
        self.assertIn("profiles", d)
        self.assertIsInstance(d["profiles"], list)

    def test_spawn_defaults_endpoint(self):
        st, body, _ = self._get("/api/v1/spawn-defaults")
        d = json.loads(body)["data"]["defaults"]
        self.assertIn("provider", d)
        self.assertIn("configured", d["provider"])

    def test_spawn_challenge_root_fans_out(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "chalA"))
            os.mkdir(os.path.join(root, "chalB"))
            os.mkdir(os.path.join(root, ".git"))  # dotfolder skipped
            open(os.path.join(root, "readme.txt"), "w").close()  # non-folder skipped
            st, j = self._req("POST", "/api/v1/runs",
                              {"challenge_root": root, "provider": "gcp", "agent_backend": "codex"})
            self.assertEqual(st, 200)
            self.assertEqual(len(j["data"]["job_ids"]), 2)  # one VM per real subfolder

    def test_spawn_empty_root_is_400(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            st, j = self._req("POST", "/api/v1/runs", {"challenge_root": root})
            self.assertEqual(st, 400)

    def test_spawn_bad_batch_is_400(self):
        st, j = self._req("POST", "/api/v1/runs", {"batch": "not-a-list"})
        self.assertEqual(st, 400)
        self.assertEqual(j["code"], "BAD_REQUEST")
        st, j = self._req("POST", "/api/v1/runs", {})  # missing challenge_dir
        self.assertEqual(st, 400)

    def test_malformed_content_length_does_not_crash(self):
        # send a bad Content-Length and assert the server still responds
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.putrequest("GET", "/health", skip_host=False, skip_accept_encoding=True)
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        conn.close()

    def test_not_found_for_removed_routes(self):
        for path in ("/overview", "/api/snapshot", "/api/v1/bogus", "/api/select-directory"):
            st, j = self._req("GET", path)
            self.assertEqual(st, 404, path)
            self.assertEqual(j["code"], "NOT_FOUND")

    def test_frontend_served(self):
        st, body, hdr = self._get("/")
        self.assertEqual(st, 200)
        self.assertIn("text/html", hdr.get("Content-Type", ""))
        self.assertIn(b"clanker", body)
        # the operational redesign elements are present
        for marker in (b"submitSteer", b"steerKey", b"hexDump", b"Approve",
                       b"steer-target", b"steerSubagent", b"browseDir", b"select-directory",
                       b"sp-desc", b"sp-novpn", b"status-note",
                       b"providerFields", b"sp-region", b"sp-do-fields", b"sp-gcp-fields",
                       b"sp-account", b"loadProfiles", b"prefillSpawn", b"loadDefaults", b"spawn-defaults",
                       b"vpnrow", b"loadVpnStatus"):
            self.assertIn(marker, body, marker)


class VpnStatus(unittest.TestCase):
    def test_connected_vs_not(self):
        import clanker.server.service as svc
        from unittest.mock import patch
        with TemporaryDirectory() as root, TemporaryDirectory() as runs:
            service = _make_service(Path(runs))
            with patch.object(svc, "ROOT", Path(root)):
                self.assertFalse(service.vpn_status("R1")["connected"])
                self.assertIn("vpn --run-id R1 up", service.vpn_status("R1")["up_command"])
                vd = Path(root) / ".ctfvm" / "vpn" / "R1"
                vd.mkdir(parents=True)
                (vd / "wg0.json").write_text(json.dumps({
                    "run_id": "R1", "local_interface": "wg0", "local_ip": "10.88.1.1",
                    "remote_ip": "10.88.1.2", "local_cidrs": ["192.168.0.0/16"], "nat_enabled": True,
                }))
                d = service.vpn_status("R1")
        self.assertTrue(d["connected"])
        self.assertEqual(d["interface"], "wg0")
        self.assertIn("192.168.0.0/16", d["cidrs"])
        self.assertTrue(d["nat"])

    def test_vpn_requested_intent(self):
        with TemporaryDirectory() as runs:
            runs_dir = Path(runs) / "runs"
            runs_dir.mkdir(parents=True)
            common = {"provider": "digitalocean", "zone": "sgp1", "project": "p"}
            (runs_dir / "novpn.json").write_text(json.dumps(
                {**common, "run_id": "novpn", "instance": "ctfvm-x-novpn", "vpn_requested": "0"}))
            (runs_dir / "withvpn.json").write_text(json.dumps(
                {**common, "run_id": "withvpn", "instance": "ctfvm-x-withvpn", "vpn_requested": "1"}))
            (runs_dir / "legacy.json").write_text(json.dumps(
                {**common, "run_id": "legacy", "instance": "ctfvm-x-legacy"}))  # no field
            reg = RunRegistry(runs_dir=runs_dir, state_file=Path(runs) / "cur.json",
                              status=lambda r: "RUNNING")
            service = UiService(registry=reg, providers=object(),
                                jobs=_FakeTracker(now=lambda: "t"), client_factory=lambda r: FakeClient())
            self.assertFalse(service.vpn_status("novpn")["requested"])
            self.assertTrue(service.vpn_status("withvpn")["requested"])
            self.assertTrue(service.vpn_status("legacy")["requested"])  # defaults on


class DnsProxyStatus(unittest.TestCase):
    """The top-bar chip reads dns_proxy_status: count tunnels with a forwarder
    pidfile, and how many of those processes are alive."""
    def test_counts_running_vs_expected(self):
        import os
        import subprocess
        import clanker.server.service as svc
        from unittest.mock import patch
        from clanker.server.jobs import SpawnJobTracker
        with TemporaryDirectory() as root:
            vpn = Path(root) / ".ctfvm" / "vpn"
            a = vpn / "runA"; a.mkdir(parents=True)
            (a / "wgA.json").write_text("{}")
            (a / "wgA-dns.pid").write_text(str(os.getpid()))            # alive (this process)
            b = vpn / "runB"; b.mkdir(parents=True)
            (b / "wgB.json").write_text("{}")
            dead = subprocess.Popen(["true"]); dead.wait()              # reaped -> dead pid
            (b / "wgB-dns.pid").write_text(str(dead.pid))
            c = vpn / "runC"; c.mkdir(parents=True)                     # tunnel down (no .json) -> ignored
            (c / "wgC-dns.pid").write_text(str(os.getpid()))
            service = UiService(registry=object(), providers=object(),
                                jobs=SpawnJobTracker(now=lambda: "t"), client_factory=lambda r: None)
            with patch.object(svc, "ROOT", Path(root)):
                st = service.dns_proxy_status()
        self.assertEqual(st["expected"], 2)   # runA + runB; runC ignored (no tunnel state)
        self.assertEqual(st["running"], 1)    # only runA alive


class SpawnDefaultsResolve(unittest.TestCase):
    def test_configured_vs_default(self):
        import clanker.server.service as svc
        from unittest.mock import patch
        from clanker.config import Settings as RealSettings
        with TemporaryDirectory() as env_root, TemporaryDirectory() as runs_root:
            (Path(env_root) / ".env").write_text(
                "CTFVM_PROVIDER=digitalocean   # inline comment\nCTFVM_DO_REGION=nyc3\n")
            service = _make_service(Path(runs_root))
            with patch.object(svc, "Settings", lambda *a, **k: RealSettings(root=Path(env_root))):
                d = service.spawn_defaults()
        self.assertEqual(d["provider"]["value"], "digitalocean")  # comment stripped
        self.assertTrue(d["provider"]["configured"])
        self.assertEqual(d["do_region"]["value"], "nyc3")
        self.assertTrue(d["do_region"]["configured"])
        # a key left unset is not "configured" (stays a placeholder in the form)
        self.assertFalse(d["gcp_project"]["configured"])
        self.assertFalse(d["gcp_machine_type"]["configured"])  # schema default != configured


class DirectoryPicker(unittest.TestCase):
    def _svc(self):
        with TemporaryDirectory() as tmp:
            return _make_service(Path(tmp))

    def test_rejects_non_macos(self):
        import os as _os
        from unittest.mock import patch
        from clanker.server.service import ApiError
        svc = self._svc()
        Uname = type("U", (), {"sysname": "Linux"})
        with patch.object(_os, "uname", lambda: Uname()):
            with self.assertRaises(ApiError) as cm:
                svc.choose_directory()
            self.assertEqual(cm.exception.code, "BAD_REQUEST")

    def test_returns_selected_path(self):
        import os as _os
        import subprocess as _sp
        from unittest.mock import patch
        svc = self._svc()
        Uname = type("U", (), {"sysname": "Darwin"})
        with TemporaryDirectory() as chal:
            (Path(chal) / "description.txt").write_text("a heap chal\n")
            completed = type("C", (), {"returncode": 0, "stdout": chal + "\n", "stderr": ""})
            with patch.object(_os, "uname", lambda: Uname()), \
                 patch("shutil.which", lambda _x: "/usr/bin/osascript"), \
                 patch.object(_sp, "run", lambda *a, **k: completed()):
                out = svc.choose_directory()
        self.assertEqual(out["path"], chal)
        self.assertEqual(out["description"], "a heap chal")
        self.assertFalse(out["canceled"])

    def test_cancel_is_not_an_error(self):
        import os as _os
        import subprocess as _sp
        from unittest.mock import patch
        svc = self._svc()
        Uname = type("U", (), {"sysname": "Darwin"})
        canceled = type("C", (), {"returncode": 1, "stdout": "", "stderr": "User canceled. (-128)"})
        with patch.object(_os, "uname", lambda: Uname()), \
             patch("shutil.which", lambda _x: "/usr/bin/osascript"), \
             patch.object(_sp, "run", lambda *a, **k: canceled()):
            out = svc.choose_directory()
        self.assertTrue(out["canceled"])


class UiAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.app = App(_make_service(Path(self.tmp.name)), auth_token="s3cret")

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocks_without_token(self):
        r = self.app.handle("GET", "/api/v1/runs", {}, b"", {})
        self.assertEqual(r.status, 401)

    def test_health_open(self):
        r = self.app.handle("GET", "/health", {}, b"", {})
        self.assertEqual(r.status, 200)

    def test_query_token_sets_cookie(self):
        r = self.app.handle("GET", "/", {"token": ["s3cret"]}, b"", {})
        self.assertEqual(r.status, 200)
        self.assertIn("clanker_token=s3cret", (r.headers or {}).get("Set-Cookie", ""))

    def test_cookie_and_bearer_accepted(self):
        r = self.app.handle("GET", "/api/v1/runs", {}, b"", {"Cookie": "clanker_token=s3cret"})
        self.assertEqual(r.status, 200)
        r = self.app.handle("GET", "/api/v1/runs", {}, b"", {"Authorization": "Bearer s3cret"})
        self.assertEqual(r.status, 200)

    def test_wrong_token_blocked(self):
        r = self.app.handle("GET", "/api/v1/runs", {"token": ["nope"]}, b"", {})
        self.assertEqual(r.status, 401)

    def test_no_token_configured_is_open(self):
        open_app = App(_make_service(Path(self.tmp.name)))  # no auth_token
        r = open_app.handle("GET", "/api/v1/runs", {}, b"", {})
        self.assertEqual(r.status, 200)


class Share(unittest.TestCase):
    def test_public_link_and_token(self):
        from clanker.config import Settings
        from clanker.share import ensure_ui_token, public_link
        self.assertEqual(public_link("https://x.ngrok.app/", "T"), "https://x.ngrok.app/?token=T")
        # configured token is reused; otherwise an ephemeral one is minted
        self.assertEqual(ensure_ui_token(Settings(cli={"ui_token": "fixed"})), "fixed")
        self.assertGreaterEqual(len(ensure_ui_token(Settings(cli={}))), 16)


class JobsTracker(unittest.TestCase):
    def test_run_subprocess_detects_run_id(self):
        import time

        from clanker.server.jobs import SpawnJobTracker
        tr = SpawnJobTracker(now=lambda: "t")
        job = tr.submit(["bash", "-c", "echo creating run 20250101-000000; exit 0"])
        for _ in range(100):
            if tr.get(job.job_id).state in ("done", "error"):
                break
            time.sleep(0.02)
        done = tr.get(job.job_id)
        self.assertEqual(done.state, "done")
        self.assertEqual(done.run_id, "20250101-000000")

    def test_limit_enforced(self):
        from clanker.server.jobs import JobLimitError, SpawnJobTracker
        tr = SpawnJobTracker(max_jobs=1)
        # a never-finishing job holds the only slot
        tr.submit(["bash", "-c", "sleep 5"])
        with self.assertRaises(JobLimitError):
            tr.submit(["bash", "-c", "sleep 5"])

    def test_spawn_is_detached_into_new_session(self):
        # The provision must survive the server dying: launched in its own
        # session (start_new_session) with file-backed output, not a pipe.
        import time as _t
        from unittest.mock import patch
        from clanker.server.jobs import SpawnJobTracker
        captured = {}

        class _FakeProc:
            returncode = 0
            def poll(self):
                return 0

        def fake_popen(cmd, stdout=None, stderr=None, start_new_session=False):
            captured["new_session"] = start_new_session
            captured["is_file"] = hasattr(stdout, "write") and not hasattr(stdout, "recv")
            stdout.write(b"creating run 20250101-000000\n")
            stdout.flush()
            return _FakeProc()

        tr = SpawnJobTracker(now=lambda: "t")
        with patch("clanker.server.jobs.subprocess.Popen", fake_popen):
            job = tr.submit(["ctfvm", "start"])
            for _ in range(100):
                if tr.get(job.job_id).state in ("done", "error"):
                    break
                _t.sleep(0.02)
        self.assertTrue(captured.get("new_session"), "must use start_new_session=True")
        self.assertTrue(captured.get("is_file"), "output must go to a file, not a pipe")
        self.assertEqual(tr.get(job.job_id).state, "done")
        self.assertEqual(tr.get(job.job_id).run_id, "20250101-000000")

    def test_for_run_matches_detected_run_id(self):
        from clanker.server.jobs import SpawnJob, SpawnJobTracker
        tr = SpawnJobTracker(now=lambda: "t")
        tr._jobs = {"job-0001": SpawnJob("job-0001", run_id="20260605-015240", output="waiting"),
                    "job-0002": SpawnJob("job-0002", run_id="other")}
        tr._order = ["job-0001", "job-0002"]
        matched = tr.for_run("20260605-015240")
        self.assertEqual([j.job_id for j in matched], ["job-0001"])
        self.assertEqual(tr.for_run(""), [])


class RunIdAssignment(unittest.TestCase):
    """A folder deploy must not collide on a shared second-granularity run_id."""
    @staticmethod
    def _fixed_now():
        from datetime import datetime, timezone
        return lambda: datetime(2026, 6, 5, 2, 10, 53, tzinfo=timezone.utc)

    def _svc(self, existing=()):
        from types import SimpleNamespace
        from clanker.server.jobs import SpawnJobTracker
        reg = SimpleNamespace(list_runs=lambda *a, **k: (
            [SimpleNamespace(record=SimpleNamespace(run_id=r)) for r in existing], None))
        return UiService(registry=reg, providers=object(),
                         jobs=SpawnJobTracker(now=lambda: "t"), client_factory=lambda r: None)

    def test_fanout_gets_distinct_run_ids(self):
        specs = [{"challenge_dir": f"/c/{i}"} for i in range(4)]
        self._svc()._assign_run_ids(specs, now=self._fixed_now())
        rids = [s["run_id"] for s in specs]
        self.assertEqual(len(set(rids)), 4, rids)
        self.assertEqual(rids[0], "20260605-021053")  # base second
        self.assertTrue(all(__import__("re").match(r"^\d{8}-\d{6}$", r) for r in rids))

    def test_caller_supplied_run_id_preserved_and_no_collision(self):
        specs = [{"run_id": "20260605-021053"}, {"challenge_dir": "/c/x"}]
        self._svc()._assign_run_ids(specs, now=self._fixed_now())
        self.assertEqual(specs[0]["run_id"], "20260605-021053")
        self.assertNotEqual(specs[1]["run_id"], "20260605-021053")

    def test_concurrent_batches_dont_collide(self):
        # same folder for codex then claude, same second -> distinct run_ids
        svc = self._svc()
        a = [{"challenge_dir": "/c/01"}, {"challenge_dir": "/c/02"}]
        svc._assign_run_ids(a, now=self._fixed_now())
        b = [{"challenge_dir": "/c/01"}, {"challenge_dir": "/c/02"}]
        svc._assign_run_ids(b, now=self._fixed_now())  # same base time
        allrids = [s["run_id"] for s in a + b]
        self.assertEqual(len(set(allrids)), 4, allrids)

    def test_dedups_against_existing_runs(self):
        specs = [{"challenge_dir": "/c/01"}]
        self._svc(existing=["20260605-021053"])._assign_run_ids(specs, now=self._fixed_now())
        self.assertNotEqual(specs[0]["run_id"], "20260605-021053")

    def test_build_start_cmd_passes_run_id_flag(self):
        cmd = UiService._build_start_cmd({"challenge_dir": "/c/x", "run_id": "20260605-021053",
                                          "agent_backend": "codex"})
        self.assertIn("--run-id", cmd)
        self.assertEqual(cmd[cmd.index("--run-id") + 1], "20260605-021053")


class RunNaming(unittest.TestCase):
    """Runs are named after the challenge folder; the parent disambiguates dups."""
    def _svc(self, existing=()):
        from types import SimpleNamespace
        from clanker.server.jobs import SpawnJobTracker
        reg = SimpleNamespace(list_runs=lambda *a, **k: (
            [SimpleNamespace(record=SimpleNamespace(challenge_name=n)) for n in existing], None))
        return UiService(registry=reg, providers=object(),
                         jobs=SpawnJobTracker(now=lambda: "t"), client_factory=lambda r: None)

    def test_distinct_basenames(self):
        specs = [{"challenge_dir": "/c/01-strings"}, {"challenge_dir": "/c/02-base64"}]
        self._svc()._assign_names(specs)
        self.assertEqual([s["name"] for s in specs], ["01-strings", "02-base64"])

    def test_collision_against_existing_uses_parent(self):
        specs = [{"challenge_dir": "/ctf/web/01"}]
        self._svc(existing=["01"])._assign_names(specs)
        self.assertEqual(specs[0]["name"], "web-01")

    def test_within_batch_collision_uses_parent(self):
        specs = [{"challenge_dir": "/ctf/web/01"}, {"challenge_dir": "/ctf/pwn/01"}]
        self._svc()._assign_names(specs)
        self.assertEqual([s["name"] for s in specs], ["01", "pwn-01"])

    def test_caller_supplied_name_preserved(self):
        specs = [{"challenge_dir": "/c/x", "name": "custom"}]
        self._svc()._assign_names(specs)
        self.assertEqual(specs[0]["name"], "custom")

    def test_trailing_slash_handled(self):
        specs = [{"challenge_dir": "/c/01-strings/"}]
        self._svc()._assign_names(specs)
        self.assertEqual(specs[0]["name"], "01-strings")


class ChallengeStateCache(unittest.TestCase):
    """snapshot() caches challenge_state so /runs can render pills with no
    per-run control-plane call."""
    def test_snapshot_populates_cache(self):
        with TemporaryDirectory() as runs:
            service = _make_service(Path(runs))
            self.assertIsNone(service.cached_challenge_state("20250101-000000"))
            service.snapshot("20250101-000000")
            cs = service.cached_challenge_state("20250101-000000")
            self.assertIsNotNone(cs)
            self.assertEqual(cs.state, "progressing")


class ProvisioningRelabel(unittest.TestCase):
    """A still-starting run reads as Stopped/Halted from derive_challenge_state;
    while young (or a job is still working it) it should show Provisioning."""
    def _svc(self, tracker=None):
        from clanker.server.jobs import SpawnJobTracker
        return UiService(registry=object(), providers=object(),
                         jobs=tracker or SpawnJobTracker(now=lambda: "t"),
                         client_factory=lambda r: None)

    def _snap(self, state, run_id, started_at=""):
        from clanker.models import ChallengeState, RunRecord, Snapshot
        rec = RunRecord(provider="digitalocean", run_id=run_id, instance=f"ctfvm-x-{run_id}",
                        zone="sgp1", project="p", started_at=started_at)
        cs = ChallengeState(state=state, label=state.title(), summary="")
        return Snapshot(record=rec, challenge_state=cs), rec

    def test_young_halted_becomes_provisioning(self):
        from clanker.server.service import _iso_now
        snap, rec = self._snap("halted", "20260605-125306", started_at=_iso_now())
        out = self._svc()._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "provisioning")
        self.assertEqual(out.challenge_state.label, "Provisioning")

    def test_old_halted_stays_halted(self):
        snap, rec = self._snap("halted", "20200101-000000", started_at="2020-01-01T00:00:00Z")
        out = self._svc()._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "halted")

    def test_active_job_forces_provisioning_even_if_old(self):
        from clanker.server.jobs import SpawnJob, SpawnJobTracker
        tr = SpawnJobTracker(now=lambda: "t")
        tr._jobs = {"job-0001": SpawnJob("job-0001", state="running", run_id="20200101-000000")}
        tr._order = ["job-0001"]
        snap, rec = self._snap("stopped", "20200101-000000", started_at="2020-01-01T00:00:00Z")
        out = self._svc(tr)._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "provisioning")

    def test_progressing_is_untouched(self):
        from clanker.server.service import _iso_now
        snap, rec = self._snap("progressing", "20260605-125306", started_at=_iso_now())
        out = self._svc()._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "progressing")

    def test_young_halted_with_panes_stays_halted(self):
        # agent launched (tmux pane exists) then died/dropped to a shell -> real
        # halt, must NOT be masked as Provisioning even while young.
        from clanker.models import PaneSnapshot
        from clanker.server.service import _iso_now
        snap, rec = self._snap("halted", "20260605-125306", started_at=_iso_now())
        snap.panes = [PaneSnapshot(session="ctf", window_index=0, window_name="supervisor",
                                   active=True, target="ctf:0", output="claude CLI not found")]
        out = self._svc()._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "halted")

    def test_failed_job_surfaces_error_not_provisioning(self):
        # a young run whose spawn job ERRORED must show Failed + the reason,
        # not an eternal "Provisioning".
        from clanker.server.jobs import SpawnJob, SpawnJobTracker
        from clanker.server.service import _iso_now
        tr = SpawnJobTracker(now=lambda: "t")
        tr._jobs = {"job-0001": SpawnJob(
            "job-0001", state="error", run_id="20260605-125306",
            output="Pulling toolbox image: registry.example/ctf-toolbox:lean\n"
                   "Error: GET .../docker-credentials: 404 registry not configured for user\n")}
        tr._order = ["job-0001"]
        snap, rec = self._snap("halted", "20260605-125306", started_at=_iso_now())
        out = self._svc(tr)._relabel_if_provisioning(snap, rec)
        self.assertEqual(out.challenge_state.state, "failed")
        self.assertIn("Provisioning failed:", out.error)
        self.assertIn("registry not configured", out.error)


class ProvisioningSnapshot(unittest.TestCase):
    """A run with no control plane surfaces its spawn jobs' live output instead
    of just the bare credentials error."""
    def test_snapshot_includes_provisioning_output(self):
        from clanker.controlclient import ControlPlaneError
        from clanker.server.jobs import SpawnJob, SpawnJobTracker
        from clanker.state import RunRegistry

        with TemporaryDirectory() as d:
            tmp = Path(d)
            runs = tmp / "runs"
            runs.mkdir(parents=True, exist_ok=True)
            # a run record with NO control_user/password -> no control plane
            (runs / "20260605-015240.json").write_text(json.dumps({
                "provider": "digitalocean", "run_id": "20260605-015240",
                "instance": "ctfvm-c-01-strings-20260605-015240", "zone": "sgp1",
                "project": "jloh-containers", "ip": "",
            }))
            registry = RunRegistry(runs_dir=runs, state_file=tmp / "current.json",
                                   status=lambda r: "PROVISIONING")
            tracker = SpawnJobTracker(now=lambda: "t")
            tracker._jobs = {"job-0001": SpawnJob(
                "job-0001", state="running", run_id="20260605-015240",
                started_at="t", output="Waiting for VM control plane availability...\n")}
            tracker._order = ["job-0001"]

            def boom(_rec):
                raise ControlPlaneError("run '20260605-015240' has no control-plane credentials")

            service = UiService(registry=registry, providers=object(),
                                jobs=tracker, client_factory=boom)
            snap = service.snapshot("20260605-015240")
            self.assertIn("has no control-plane credentials", snap.error)
            self.assertEqual(len(snap.provisioning), 1)
            self.assertEqual(snap.provisioning[0].job_id, "job-0001")
            self.assertEqual(snap.provisioning[0].state, "running")
            self.assertIn("Waiting for VM control plane availability", snap.provisioning[0].output_tail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
