"""`clanker share` — expose the UI via ngrok with a token baked into the link.

For temporary CTF VMs: generates an ephemeral UI token (unless one is configured
via CTFVM_UI_TOKEN), starts the authed server, launches ngrok, reads the public
URL from ngrok's local API, and prints a ready-to-open link
(`https://<public>/?token=<token>`). The `?token` sets a cookie on first load, so
every subsequent request just works.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from .config import Settings
from .server.app import App, make_handler
from .server.service import UiService

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def ensure_ui_token(settings: Settings | None = None) -> str:
    """Use a configured token (CTFVM_UI_TOKEN / secrets) if present, else mint an
    ephemeral one for this session (not persisted — temporary by design)."""
    settings = settings or Settings()
    configured = str(settings.get("ui_token") or "").strip()
    return configured or secrets.token_urlsafe(24)


def public_link(public_url: str, token: str) -> str:
    return f"{public_url.rstrip('/')}/?token={token}"


def query_ngrok_url(api: str = NGROK_API) -> str:
    """Return the https public_url from ngrok's local API, or '' if unavailable."""
    try:
        with urllib.request.urlopen(api, timeout=2) as r:
            data = json.loads(r.read())
    except Exception:
        return ""
    tunnels = data.get("tunnels") or []
    https = [t.get("public_url", "") for t in tunnels if str(t.get("proto")) == "https"]
    any_url = [t.get("public_url", "") for t in tunnels]
    return (https or any_url or [""])[0]


def run_share(host: str = "127.0.0.1", port: int = 8765, *, settings: Settings | None = None) -> int:
    settings = settings or Settings()
    token = ensure_ui_token(settings)

    app = App(UiService(), auth_token=token)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"clanker UI on http://{host}:{port} (token required)")

    try:
        ngrok = subprocess.Popen(["ngrok", "http", str(port)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        sys.stderr.write("ngrok not found on PATH. Install ngrok, then re-run `clanker share`.\n")
        sys.stderr.write("Local link (token embedded):\n  " + public_link(f"http://{host}:{port}", token) + "\n")
        return 1

    url = ""
    for _ in range(40):
        url = query_ngrok_url()
        if url:
            break
        time.sleep(0.5)

    target = url or f"http://{host}:{port}"
    if not url:
        sys.stderr.write("Could not read the ngrok public URL from http://127.0.0.1:4040 — is ngrok running?\n")
    print("\n  Share this link (token embedded, sets a cookie on first open):\n")
    print("    " + public_link(target, token) + "\n")
    print("  Ctrl-C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        ngrok.terminate()
        httpd.shutdown()
    return 0
