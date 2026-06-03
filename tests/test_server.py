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

    def test_inject_queue(self):
        st, j = self._req("POST", "/api/v1/runs/20250101-000000/inject", {"text": "hint"})
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

    def test_agents_endpoint(self):
        st, body, _ = self._get("/api/v1/agents")
        names = [a["name"] for a in json.loads(body)["data"]["agents"]]
        self.assertIn("codex", names)
        self.assertIn("claude-code", names)

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
        for marker in (b"submitSteer", b"steerKey", b"name=smode", b"/inject", b"hexDump", b"Approve",
                       b"steer-target", b"steerSubagent", b"browseDir", b"select-directory",
                       b"sp-desc", b"sp-novpn", b"status-note",
                       b"providerFields", b"sp-region", b"sp-do-fields", b"sp-gcp-fields"):
            self.assertIn(marker, body, marker)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
