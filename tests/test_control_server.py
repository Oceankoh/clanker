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
        self.assertEqual(c.getresponse().status, 401)

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
        self.assertEqual(c.getresponse().status, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
