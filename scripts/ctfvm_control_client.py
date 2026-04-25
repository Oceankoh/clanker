#!/usr/bin/env python3
from __future__ import annotations
import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_PORT = 443


def control_endpoint_from_state(state: dict) -> str:
    scheme = str(state.get("control_scheme", "") or "http").strip()
    host = str(state.get("control_host", "") or state.get("ip", "") or "").strip()
    port = str(state.get("control_port", "") or str(DEFAULT_PORT)).strip()
    if not host:
        return ""
    return f"{scheme}://{host}:{port}"


def control_credentials_from_state(state: dict) -> tuple[str, str]:
    return (
        str(state.get("control_user", "") or "").strip(),
        str(state.get("control_password", "") or "").strip(),
    )


def _auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    method: str,
    url: str,
    user: str,
    password: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 20,
) -> tuple[int, bytes]:
    headers = {"Authorization": _auth_header(user, password)}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200) or 200), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code or 500), exc.read()
    except urllib.error.URLError as exc:
        return 599, str(exc.reason or exc).encode("utf-8", errors="replace")


def control_plane_health(endpoint: str, user: str, password: str, timeout: int = 10) -> tuple[bool, dict]:
    status, body = _request("GET", endpoint.rstrip("/") + "/healthz", user, password, timeout=timeout)
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        payload = {"ok": False, "error": body.decode("utf-8", errors="replace")}
    return status == 200 and bool(payload.get("ok")), payload


def control_plane_exec(
    endpoint: str,
    user: str,
    password: str,
    command: str,
    *,
    stdin_bytes: bytes = b"",
    timeout: int = 20,
) -> tuple[int, bytes, bytes]:
    payload = json.dumps(
        {
            "command": command,
            "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii"),
            "timeout": int(timeout),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    status, body = _request(
        "POST",
        endpoint.rstrip("/") + "/exec",
        user,
        password,
        body=payload,
        content_type="application/json",
        timeout=max(timeout + 5, 10),
    )
    try:
        response = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        response = {"returncode": 1, "stdout_b64": "", "stderr_b64": base64.b64encode(body).decode("ascii")}
    if status != 200:
        message = response.get("error") or body.decode("utf-8", errors="replace") or f"http {status}"
        return 1, b"", str(message).encode("utf-8", errors="replace")
    stdout = base64.b64decode(str(response.get("stdout_b64", "") or "").encode("ascii"), validate=False)
    stderr = base64.b64decode(str(response.get("stderr_b64", "") or "").encode("ascii"), validate=False)
    return int(response.get("returncode", 1) or 0), stdout, stderr


def control_plane_upload_file(
    endpoint: str,
    user: str,
    password: str,
    remote_path: str,
    body: bytes,
    *,
    mode: str = "",
    timeout: int = 60,
) -> tuple[bool, str]:
    query = {"path": remote_path}
    if mode:
        query["mode"] = mode
    status, payload = _request(
        "POST",
        endpoint.rstrip("/") + "/files/upload?" + urllib.parse.urlencode(query),
        user,
        password,
        body=body,
        content_type="application/octet-stream",
        timeout=timeout,
    )
    try:
        response = json.loads(payload.decode("utf-8") or "{}")
    except Exception:
        response = {"ok": False, "error": payload.decode("utf-8", errors="replace")}
    return status == 200 and bool(response.get("ok")), str(response.get("error", "") or "")


def control_plane_upload_tar(
    endpoint: str,
    user: str,
    password: str,
    remote_dir: str,
    body: bytes,
    *,
    timeout: int = 120,
) -> tuple[bool, str]:
    status, payload = _request(
        "POST",
        endpoint.rstrip("/") + "/files/upload-tar?" + urllib.parse.urlencode({"dest": remote_dir}),
        user,
        password,
        body=body,
        content_type="application/octet-stream",
        timeout=timeout,
    )
    try:
        response = json.loads(payload.decode("utf-8") or "{}")
    except Exception:
        response = {"ok": False, "error": payload.decode("utf-8", errors="replace")}
    return status == 200 and bool(response.get("ok")), str(response.get("error", "") or "")


def control_plane_download_file(
    endpoint: str,
    user: str,
    password: str,
    remote_path: str,
    *,
    timeout: int = 60,
) -> tuple[bool, bytes, str]:
    status, payload = _request(
        "GET",
        endpoint.rstrip("/") + "/files/download?" + urllib.parse.urlencode({"path": remote_path}),
        user,
        password,
        timeout=timeout,
    )
    if status == 200:
        return True, payload, ""
    try:
        response = json.loads(payload.decode("utf-8") or "{}")
        error = str(response.get("error", "") or "")
    except Exception:
        error = payload.decode("utf-8", errors="replace")
    return False, b"", error or f"http {status}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CTFVM control plane client")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)

    subparsers = parser.add_subparsers(dest="command_name", required=True)

    health = subparsers.add_parser("health")
    health.add_argument("--timeout", type=int, default=10)

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--command", required=True)
    exec_parser.add_argument("--timeout", type=int, default=20)

    upload_file = subparsers.add_parser("upload-file")
    upload_file.add_argument("--path", required=True)
    upload_file.add_argument("--mode", default="")
    upload_file.add_argument("--timeout", type=int, default=60)

    upload_tar = subparsers.add_parser("upload-tar")
    upload_tar.add_argument("--dest", required=True)
    upload_tar.add_argument("--timeout", type=int, default=120)

    download = subparsers.add_parser("download-file")
    download.add_argument("--path", required=True)
    download.add_argument("--timeout", type=int, default=60)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command_name == "health":
        ok, payload = control_plane_health(args.endpoint, args.user, args.password, timeout=args.timeout)
        sys.stdout.write(json.dumps(payload))
        return 0 if ok else 1

    if args.command_name == "exec":
        stdin_bytes = sys.stdin.buffer.read()
        rc, stdout, stderr = control_plane_exec(
            args.endpoint,
            args.user,
            args.password,
            args.command,
            stdin_bytes=stdin_bytes,
            timeout=args.timeout,
        )
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        return rc

    if args.command_name == "upload-file":
        ok, error = control_plane_upload_file(
            args.endpoint,
            args.user,
            args.password,
            args.path,
            sys.stdin.buffer.read(),
            mode=args.mode,
            timeout=args.timeout,
        )
        if not ok and error:
            sys.stderr.write(error + "\n")
        return 0 if ok else 1

    if args.command_name == "upload-tar":
        ok, error = control_plane_upload_tar(
            args.endpoint,
            args.user,
            args.password,
            args.dest,
            sys.stdin.buffer.read(),
            timeout=args.timeout,
        )
        if not ok and error:
            sys.stderr.write(error + "\n")
        return 0 if ok else 1

    if args.command_name == "download-file":
        ok, payload, error = control_plane_download_file(
            args.endpoint,
            args.user,
            args.password,
            args.path,
            timeout=args.timeout,
        )
        if ok:
            sys.stdout.buffer.write(payload)
            return 0
        if error:
            sys.stderr.write(error + "\n")
        return 1

    parser.error(f"unknown command: {args.command_name}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
