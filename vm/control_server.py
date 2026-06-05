#!/usr/bin/env python3
from __future__ import annotations
import argparse
import base64
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


MAX_REQUEST_BYTES = 1_073_741_824   # file uploads
MAX_EXEC_BYTES = 10_485_760         # /exec JSON body (B5): commands are small


def _basic_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _is_within(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    candidate = candidate.resolve()
    return candidate == root or str(candidate).startswith(f"{root}{Path('/')}")


def _safe_extract_tar(archive_path: Path, dest: Path) -> None:
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive.getmembers():
            member_path = (dest / member.name).resolve()
            if not _is_within(dest, member_path):
                raise ValueError(f"refusing to extract outside destination: {member.name}")
            if member.issym() or member.islnk():
                link_name = str(member.linkname or "").strip()
                if not link_name:
                    raise ValueError(f"refusing empty link target: {member.name}")
                if Path(link_name).is_absolute():
                    raise ValueError(f"refusing absolute link target: {member.name} -> {link_name}")
                link_target = ((dest / member.name).parent / link_name).resolve()
                if not _is_within(dest, link_target):
                    raise ValueError(f"refusing link outside destination: {member.name} -> {link_name}")
        archive.extractall(path=dest)


def _run_shell(command: str, stdin_bytes: bytes, timeout: int) -> dict:
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_bytes, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        timed_out = True
    return {
        "returncode": 124 if timed_out else int(proc.returncode or 0),
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "timed_out": timed_out,
    }


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "ctfvm-control/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return  # default access log silenced; we emit a structured line per request

    def _log(self, status: int, extra: str = "") -> None:
        """Structured one-line request log to stderr (captured by the systemd
        journal). Replaces the no-op access log (B5)."""
        t0 = getattr(self, "_t0", None)
        ms = int((time.time() - t0) * 1000) if t0 else 0
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        client = self.client_address[0] if self.client_address else "-"
        sys.stderr.write(f"{ts} {client} {self.command} {self.path} {int(status)} {ms}ms {extra}".rstrip() + "\n")
        sys.stderr.flush()

    @property
    def auth_header(self) -> str:
        return self.server.auth_header

    @property
    def provider(self) -> str:
        return self.server.provider

    @property
    def started_at(self) -> float:
        return self.server.started_at

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self._log(status)

    def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self._log(status, f"{len(payload)}B")

    def _unauthorized(self) -> None:
        # We reject before reading the request body; on an HTTP/1.1 keep-alive
        # connection those unread bytes would desync the next request. Close it.
        self.close_connection = True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="ctfvm-control"')
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self._log(HTTPStatus.UNAUTHORIZED)

    def _authenticate(self) -> bool:
        if self.headers.get("Authorization", "") == self.auth_header:
            return True
        self._unauthorized()
        return False

    def _request_size(self) -> int:
        try:
            return int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return -1

    def _read_body(self, max_bytes: int = MAX_REQUEST_BYTES) -> bytes | None:
        size = self._request_size()
        if size < 0:
            # we won't read the body -> close to avoid desyncing a keep-alive conn
            self.close_connection = True
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid content-length"})
            return None
        if size > max_bytes:
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "request too large"})
            return None
        return self.rfile.read(size)

    def _parse_request_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict | None:
        body = self._read_body(max_bytes)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except Exception:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return None

    def do_GET(self) -> None:
        self._t0 = time.time()
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "provider": self.provider,
                    "uptime_sec": max(int(time.time() - self.started_at), 0),
                },
            )
            return

        if parsed.path == "/files/download":
            if not self._authenticate():
                return
            params = parse_qs(parsed.query)
            path_text = str((params.get("path") or [""])[0] or "").strip()
            if not path_text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "path is required"})
                return
            target = Path(path_text)
            if not target.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "file not found"})
                return
            self._send_bytes(HTTPStatus.OK, target.read_bytes(), "application/octet-stream")
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        self._t0 = time.time()
        parsed = urlparse(self.path)
        if parsed.path == "/exec":
            if not self._authenticate():
                return
            payload = self._parse_request_json(max_bytes=MAX_EXEC_BYTES)
            if payload is None:
                return
            command = str(payload.get("command", "") or "").strip()
            if not command:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "command is required"})
                return
            timeout = payload.get("timeout", 20)
            try:
                timeout = max(1, min(int(timeout), 3600))
            except Exception:
                timeout = 20
            try:
                stdin_bytes = base64.b64decode(str(payload.get("stdin_b64", "") or "").encode("ascii"), validate=False)
            except Exception:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid stdin payload"})
                return
            self._send_json(HTTPStatus.OK, _run_shell(command, stdin_bytes, timeout))
            return

        if parsed.path == "/files/upload":
            if not self._authenticate():
                return
            params = parse_qs(parsed.query)
            path_text = str((params.get("path") or [""])[0] or "").strip()
            if not path_text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "path is required"})
                return
            mode_text = str((params.get("mode") or [""])[0] or "").strip()
            body = self._read_body()
            if body is None:
                return
            target = Path(path_text)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            if mode_text:
                try:
                    target.chmod(int(mode_text, 8))
                except Exception:
                    pass
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "path": str(target), "size": len(body)},
            )
            return

        if parsed.path == "/files/upload-tar":
            if not self._authenticate():
                return
            params = parse_qs(parsed.query)
            dest_text = str((params.get("dest") or [""])[0] or "").strip()
            if not dest_text:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "dest is required"})
                return
            body = self._read_body()
            if body is None:
                return
            dest = Path(dest_text)
            temp_dir = Path(tempfile.mkdtemp(prefix="ctfvm-control-"))
            archive_path = temp_dir / "upload.tar"
            try:
                archive_path.write_bytes(body)
                _safe_extract_tar(archive_path, dest)
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            self._send_json(HTTPStatus.OK, {"ok": True, "dest": str(dest)})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="CTFVM HTTP control plane")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--provider", default="unknown")
    args = parser.parse_args()

    class ControlPlaneServer(ThreadingHTTPServer):
        daemon_threads = True

    server = ControlPlaneServer((args.host, args.port), ControlPlaneHandler)
    server.auth_header = _basic_header(args.user, args.password)
    server.provider = args.provider
    server.started_at = time.time()
    server.serve_forever()


if __name__ == "__main__":
    main()
