"""Phase 6: VM control server hardening (auth, /exec cap, exec round-trip)."""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "vm"))

import control_server  # noqa: E402

USER, PW = "u", "pw"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PW}".encode()).decode()


class ControlServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = TemporaryDirectory()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), control_server.ControlPlaneHandler)
        httpd.auth_header = control_server._basic_header(USER, PW)
        httpd.provider = "test"
        httpd.started_at = __import__("time").time()
        cls.httpd = httpd
        cls.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls._tmp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port)

    def test_healthz_no_auth(self):
        c = self._conn()
        c.request("GET", "/healthz")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        body = json.loads(r.read())
        self.assertTrue(body["ok"])
        self.assertEqual(body["provider"], "test")

    def test_exec_requires_auth(self):
        c = self._conn()
        c.request("POST", "/exec", json.dumps({"command": "echo hi"}),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 401)
        # body wasn't drained -> must close the connection to avoid keep-alive desync
        self.assertEqual((r.getheader("Connection") or "").lower(), "close")

    def test_exec_runs_command(self):
        c = self._conn()
        c.request("POST", "/exec", json.dumps({"command": "echo hi"}),
                  {"Content-Type": "application/json", "Authorization": AUTH})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        body = json.loads(r.read())
        self.assertEqual(body["returncode"], 0)
        self.assertEqual(base64.b64decode(body["stdout_b64"]).decode(), "hi\n")

    def test_exec_body_cap(self):
        # claim a body larger than MAX_EXEC_BYTES -> 413 before any body read
        c = self._conn()
        c.putrequest("POST", "/exec")
        c.putheader("Authorization", AUTH)
        c.putheader("Content-Type", "application/json")
        c.putheader("Content-Length", str(control_server.MAX_EXEC_BYTES + 1))
        c.endheaders()
        c.send(b"{}")  # small actual body; server rejects on declared size
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        # connection is closed on reject so a queued body can't desync keep-alive
        self.assertEqual((r.getheader("Connection") or "").lower(), "close")


class ControlClientDecode(unittest.TestCase):
    """The typed control client must surface a malformed /exec payload as a
    ControlPlaneError, not an uncaught binascii.Error."""
    def test_bad_base64_raises_control_plane_error(self):
        sys.path.insert(0, str(REPO_ROOT))
        from clanker.controlclient import ControlPlaneClient, ControlPlaneError
        c = ControlPlaneClient("http://x", "u", "p")
        # 5 base64 chars is an invalid length -> b64decode raises
        c._request = lambda *a, **k: (200, b'{"returncode":0,"stdout_b64":"AAAAA","stderr_b64":""}')
        with self.assertRaises(ControlPlaneError):
            c.exec("echo hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
