"""Typed HTTP control-plane client.

Replaces the stringly-typed ``scripts/ctfvm_control_client.py``. The wire
protocol is identical (HTTP Basic Auth; base64 stdin/stdout/stderr on ``/exec``;
raw bodies for file transfer), but this client:

  * returns typed results (``ExecResult``) instead of tuples,
  * raises typed exceptions instead of silently mapping failures to
    ``returncode=1`` (fixes BUGS.md B8),
  * applies an explicit timeout to every call and retries transient transport
    errors once.
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .models import ExecResult, RunRecord


class ControlPlaneError(Exception):
    """Base class for all control-plane failures."""


class ControlPlaneAuthError(ControlPlaneError):
    """401 — bad or missing Basic Auth credentials."""


class ControlPlaneTimeout(ControlPlaneError):
    """The request exceeded its timeout."""


class ControlPlaneClient:
    def __init__(self, endpoint: str, user: str, password: str, *, retries: int = 1):
        endpoint = (endpoint or "").rstrip("/")
        if not endpoint:
            raise ControlPlaneError("control plane endpoint is empty")
        if not user or not password:
            raise ControlPlaneError("control plane credentials are missing")
        self.endpoint = endpoint
        self._user = user
        self._password = password
        self._retries = max(0, int(retries))

    @classmethod
    def from_run(cls, run: RunRecord, *, retries: int = 1) -> "ControlPlaneClient":
        """Build a client from a run record, or raise if it has no control plane
        (callers on the hot path must surface this, not fall back to SSH —
        Invariant 1)."""
        if not run.has_control_plane:
            raise ControlPlaneError(
                f"run {run.run_id or run.instance!r} has no control-plane credentials"
            )
        return cls(run.control_endpoint, run.control_user, run.control_password, retries=retries)

    # --- transport ---------------------------------------------------------
    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode("ascii")
        return f"Basic {token}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        timeout: int,
    ) -> tuple[int, bytes]:
        url = self.endpoint + path
        headers = {"Authorization": self._auth_header()}
        if content_type:
            headers["Content-Type"] = content_type

        attempts = self._retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return int(getattr(response, "status", 200) or 200), response.read()
            except urllib.error.HTTPError as exc:
                # HTTP-level errors are deterministic; don't retry.
                payload = exc.read() if hasattr(exc, "read") else b""
                if int(exc.code or 0) == 401:
                    raise ControlPlaneAuthError("control plane rejected credentials (401)") from exc
                return int(exc.code or 500), payload
            except socket.timeout as exc:
                raise ControlPlaneTimeout(f"control plane timed out after {timeout}s") from exc
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, socket.timeout):
                    raise ControlPlaneTimeout(f"control plane timed out after {timeout}s") from exc
                last_exc = exc  # transient — retry
                continue
        raise ControlPlaneError(f"control plane unreachable: {last_exc}")

    @staticmethod
    def _decode_json(body: bytes) -> dict:
        try:
            data = json.loads(body.decode("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # --- operations --------------------------------------------------------
    def health(self, *, timeout: int = 10) -> dict:
        status, body = self._request("GET", "/healthz", timeout=timeout)
        payload = self._decode_json(body)
        if status != 200 or not payload.get("ok"):
            raise ControlPlaneError(
                payload.get("error") or f"health check failed (http {status})"
            )
        return payload

    def exec(self, command: str, *, stdin: bytes = b"", timeout: int = 30) -> ExecResult:
        body = json.dumps(
            {
                "command": command,
                "stdin_b64": base64.b64encode(stdin).decode("ascii"),
                "timeout": int(timeout),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        status, payload = self._request(
            "POST",
            "/exec",
            body=body,
            content_type="application/json",
            timeout=max(timeout + 5, 10),
        )
        data = self._decode_json(payload)
        if status != 200:
            message = data.get("error") or payload.decode("utf-8", errors="replace") or f"http {status}"
            raise ControlPlaneError(f"/exec failed: {message}")
        if not data:
            raise ControlPlaneError("/exec returned a non-JSON body")
        stdout = base64.b64decode((data.get("stdout_b64") or "").encode("ascii"), validate=False)
        stderr = base64.b64decode((data.get("stderr_b64") or "").encode("ascii"), validate=False)
        return ExecResult(
            returncode=int(data.get("returncode", 1) or 0),
            stdout=stdout,
            stderr=stderr,
            timed_out=bool(data.get("timed_out", False)),
        )

    def upload_file(self, remote_path: str, body: bytes, *, mode: str = "", timeout: int = 60) -> None:
        query = {"path": remote_path}
        if mode:
            query["mode"] = mode
        status, payload = self._request(
            "POST",
            "/files/upload?" + urllib.parse.urlencode(query),
            body=body,
            content_type="application/octet-stream",
            timeout=timeout,
        )
        self._raise_unless_ok(status, payload, f"upload {remote_path}")

    def upload_tar(self, remote_dir: str, body: bytes, *, timeout: int = 120) -> None:
        status, payload = self._request(
            "POST",
            "/files/upload-tar?" + urllib.parse.urlencode({"dest": remote_dir}),
            body=body,
            content_type="application/octet-stream",
            timeout=timeout,
        )
        self._raise_unless_ok(status, payload, f"upload-tar {remote_dir}")

    def download_file(self, remote_path: str, *, timeout: int = 60) -> bytes:
        status, payload = self._request(
            "GET",
            "/files/download?" + urllib.parse.urlencode({"path": remote_path}),
            timeout=timeout,
        )
        if status == 200:
            return payload
        data = self._decode_json(payload)
        message = data.get("error") or payload.decode("utf-8", errors="replace") or f"http {status}"
        raise ControlPlaneError(f"download {remote_path} failed: {message}")

    @staticmethod
    def _raise_unless_ok(status: int, payload: bytes, what: str) -> None:
        data = ControlPlaneClient._decode_json(payload)
        if status == 200 and data.get("ok"):
            return
        message = data.get("error") or payload.decode("utf-8", errors="replace") or f"http {status}"
        raise ControlPlaneError(f"{what} failed: {message}")
