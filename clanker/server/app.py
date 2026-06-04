"""HTTP app: the /api/v1 router + the SPA, over UiService.

Pure stdlib (no framework). Handlers are thin: parse -> call service -> serialize
-> envelope. Errors become ``{ok:false, error, code}`` via ``ApiError``.
"""

from __future__ import annotations

import hmac
import json
import re
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import serialize
from .service import ApiError, UiService

FRONTEND = Path(__file__).resolve().parent / "frontend" / "index.html"


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json"
    headers: dict | None = None


def _json(data, status=200) -> Response:
    return Response(status, json.dumps(data).encode("utf-8"))


def ok(data, status=200) -> Response:
    return _json({"ok": True, "data": data}, status)


def err(code, message, status=400) -> Response:
    return _json({"ok": False, "error": message, "code": code}, status)


def _safe_filename(name: str) -> str:
    """Strip CR/LF and quotes so a (potentially agent-created) filename can't
    inject or break the Content-Disposition header."""
    cleaned = "".join(c for c in (name or "") if c not in '\r\n"\\').strip()
    return cleaned or "download"


def _bool(query: dict, key: str, default=False) -> bool:
    v = query.get(key, [""])[0].strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return default


COOKIE = "clanker_token"


class App:
    def __init__(self, service: UiService, *, auth_token: str = ""):
        self.s = service
        self.auth_token = (auth_token or "").strip()
        self.routes = [
            ("GET", re.compile(r"^/health$"), self._health),
            ("GET", re.compile(r"^/api/v1/agents$"), self._agents),
            ("GET", re.compile(r"^/api/v1/profiles$"), self._profiles),
            ("GET", re.compile(r"^/api/v1/spawn-defaults$"), self._spawn_defaults),
            ("GET", re.compile(r"^/api/v1/runs$"), self._runs_list),
            ("POST", re.compile(r"^/api/v1/runs$"), self._spawn),
            ("POST", re.compile(r"^/api/v1/select-directory$"), self._select_directory),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)$"), self._snapshot),
            ("POST", re.compile(r"^/api/v1/runs/([^/]+)/status$"), self._set_status),
            ("DELETE", re.compile(r"^/api/v1/runs/([^/]+)/status$"), self._clear_status),
            ("POST", re.compile(r"^/api/v1/runs/([^/]+)/panes/send$"), self._pane_send),
            ("POST", re.compile(r"^/api/v1/runs/([^/]+)/panes/keys$"), self._pane_keys),
            ("POST", re.compile(r"^/api/v1/runs/([^/]+)/panes/trust$"), self._pane_trust),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)/subagents$"), self._subagents),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)/transcript$"), self._transcript),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)/artifacts/(.+)/download$"), self._artifact_download),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)/bundle$"), self._bundle),
            ("GET", re.compile(r"^/api/v1/runs/([^/]+)/artifacts/(.+)$"), self._artifact_preview),
            ("GET", re.compile(r"^/api/v1/jobs$"), self._jobs_list),
            ("GET", re.compile(r"^/api/v1/jobs/([^/]+)$"), self._job_get),
            ("GET", re.compile(r"^/$"), self._frontend),
        ]

    def handle(self, method: str, path: str, query: dict, body: bytes, headers=None) -> Response:
        # /health stays open for liveness checks (no secrets); everything else is
        # gated when a UI token is configured.
        if self.auth_token and path != "/health":
            ok, from_query = self._check_auth(query, headers or {})
            if not ok:
                return self._auth_challenge(method, path)
            resp = self._route(method, path, query, body)
            if from_query:  # link carried the token -> drop a cookie so later requests pass
                resp.headers = {**(resp.headers or {}),
                                "Set-Cookie": f"{COOKIE}={self.auth_token}; Path=/; SameSite=Lax; HttpOnly"}
            return resp
        return self._route(method, path, query, body)

    def _route(self, method: str, path: str, query: dict, body: bytes) -> Response:
        for m, rx, fn in self.routes:
            if m != method:
                continue
            match = rx.match(path)
            if not match:
                continue
            try:
                return fn(match, query, body)
            except ApiError as e:
                return err(e.code, e.message, e.status)
            except Exception as e:  # noqa: BLE001
                return err("INTERNAL", str(e), 500)
        return err("NOT_FOUND", f"No route for {method} {path}", 404)

    def _check_auth(self, query: dict, headers) -> tuple[bool, bool]:
        tok = self.auth_token

        def eq(v: str) -> bool:
            return bool(v) and hmac.compare_digest(v, tok)

        q = (query.get("token") or [""])[0]
        if eq(q):
            return True, True
        auth = headers.get("Authorization", "") or ""
        if auth.startswith("Bearer ") and eq(auth[len("Bearer "):]):
            return True, False
        if eq(headers.get("X-Clanker-Token", "") or ""):
            return True, False
        for part in (headers.get("Cookie", "") or "").split(";"):
            part = part.strip()
            if part.startswith(f"{COOKIE}=") and eq(part[len(COOKIE) + 1:]):
                return True, False
        return False, False

    def _auth_challenge(self, method: str, path: str) -> Response:
        if method == "GET" and path == "/":
            html = (b"<!doctype html><meta charset=utf-8><title>clanker</title>"
                    b"<body style='font:14px sans-serif;padding:40px'>"
                    b"<h2>clanker</h2><p>This UI requires a token. Open the link that includes "
                    b"<code>?token=...</code> (e.g. from <code>clanker share</code>).</p></body>")
            return Response(401, html, "text/html; charset=utf-8")
        return err("UNAUTHORIZED", "missing or invalid token", 401)

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def _body_json(body: bytes) -> dict:
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            raise ApiError("BAD_REQUEST", f"invalid JSON body: {e}", 400)

    # --- handlers ----------------------------------------------------------
    def _frontend(self, *_):
        try:
            return Response(200, FRONTEND.read_bytes(), "text/html; charset=utf-8")
        except OSError:
            return Response(200, b"<h1>clanker</h1><p>frontend missing</p>", "text/html; charset=utf-8")

    def _health(self, *_):
        return ok(self.s.health())

    def _agents(self, *_):
        return ok({"agents": self.s.agents()})

    def _profiles(self, *_):
        return ok({"profiles": self.s.profiles()})

    def _spawn_defaults(self, *_):
        return ok({"defaults": self.s.spawn_defaults()})

    def _runs_list(self, _m, query, _b):
        listings, current = self.s.list_runs(
            include_status=_bool(query, "include_status"),
            force_discovery=_bool(query, "force_discovery"),
            only_live=_bool(query, "only_live"),
        )
        return ok({"runs": [serialize.run_listing(l) for l in listings], "current_run_id": current})

    def _snapshot(self, m, query, _b):
        snap = self.s.snapshot(m.group(1), include_artifacts=_bool(query, "include_artifacts", True),
                               force_status=_bool(query, "force_status"))
        return ok(serialize.snapshot(snap))

    def _spawn(self, _m, _q, body):
        return ok({"job_ids": self.s.spawn(self._body_json(body))})

    def _select_directory(self, _m, _q, body):
        data = self._body_json(body)
        return ok(self.s.choose_directory(data.get("current_path", ""), bool(data.get("batch", False))))

    def _set_status(self, m, _q, body):
        data = self._body_json(body)
        self.s.set_status(m.group(1), data.get("state", ""), data.get("note", ""))
        return ok({"run_id": m.group(1)})

    def _clear_status(self, m, _q, _b):
        self.s.clear_status(m.group(1))
        return ok({"run_id": m.group(1)})

    def _pane_send(self, m, _q, body):
        data = self._body_json(body)
        self.s.pane_send(m.group(1), data.get("target", "ctf:supervisor"),
                         data.get("text", ""), bool(data.get("press_enter", True)))
        return ok({"target": data.get("target", "ctf:supervisor")})

    def _pane_keys(self, m, _q, body):
        data = self._body_json(body)
        keys = data.get("keys", [])
        keys = keys if isinstance(keys, list) else [keys]
        self.s.pane_keys(m.group(1), data.get("target", "ctf:supervisor"), keys)
        return ok({"target": data.get("target", "ctf:supervisor"), "keys": keys})

    def _pane_trust(self, m, _q, body):
        data = self._body_json(body)
        self.s.pane_trust(m.group(1), data.get("target", "ctf:supervisor"))
        return ok({"target": data.get("target", "ctf:supervisor")})

    def _subagents(self, m, _q, _b):
        subs = self.s.subagents(m.group(1))
        return ok({"subagents": [serialize.subagent(s) for s in subs]})

    def _transcript(self, m, _q, _b):
        backend, events = self.s.transcript(m.group(1))
        return ok({"backend": backend, "events": [e.to_dict() for e in events]})

    def _artifact_preview(self, m, _q, _b):
        relpath = urllib.parse.unquote(m.group(2))
        pv = self.s.artifact_preview(m.group(1), relpath)
        return ok({
            "relpath": pv.relpath, "mime": pv.mime, "size": pv.size, "truncated": pv.truncated,
            "is_text": pv.is_text, "is_image": pv.is_image, "content": pv.content,
            "image_data_url": pv.image_data_url, "b64": pv.b64,
        })

    def _artifact_download(self, m, _q, _b):
        relpath = urllib.parse.unquote(m.group(2))
        dl = self.s.artifact_download(m.group(1), relpath)
        return Response(200, dl.content, dl.mime,
                        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(dl.filename)}"'})

    def _bundle(self, m, _q, _b):
        dl = self.s.bundle(m.group(1))
        return Response(200, dl.content, dl.mime,
                        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(dl.filename)}"'})

    def _jobs_list(self, *_):
        return ok({"jobs": [serialize.job(j) for j in self.s.jobs_list()]})

    def _job_get(self, m, _q, _b):
        return ok(serialize.job(self.s.job_get(m.group(1))))


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _dispatch(self, method: str):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            resp = app.handle(method, parsed.path, query, body, self.headers)
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            for k, v in (resp.headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765, *, service: UiService | None = None,
          auth_token: str = "") -> None:
    app = App(service or UiService(), auth_token=auth_token)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    note = " (token required)" if auth_token else ""
    print(f"clanker UI on http://{host}:{port}{note}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
