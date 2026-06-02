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
            return ExecResult(0, stdout=json.dumps(_SNAP).encode())
        if "tar -czf" in command:
            return ExecResult(0, stdout=b"/tmp/ctfvm-bundle-1.tar.gz\n")
        return ExecResult(0)

    def download_file(self, remote_path, *, timeout=60):
        return b"BUNDLE"


def _make_service(tmp: Path) -> UiService:
    runs = tmp / "runs"
    runs.mkdir(parents=True)
    (runs / "20250101-000000.json").write_text(json.dumps({
        "provider": "gcp", "run_id": "20250101-000000", "instance": "ctfvm-a-20250101-000000",
        "zone": "z", "project": "p", "ip": "1.2.3.4",
        "control_host": "1.2.3.4", "control_port": "443", "control_user": "u", "control_password": "pw",
    }))
    registry = RunRegistry(runs_dir=runs, state_file=tmp / "current.json", status=lambda r: "RUNNING")
    return UiService(registry=registry, providers=object(), client_factory=lambda rec: FakeClient())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
