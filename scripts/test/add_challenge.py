#!/usr/bin/env python3
"""Add challenge(s) to a worker via the UI API — the scripted equivalent of the
browser "Add challenge" button. Gzip-tars each challenge dir's contents (top
folder stripped) as the POST body and passes name/description/ideas/flag_format/
reasoning_effort as query params (the server builds the prompt from these).

Usage:
    scripts/test/add_challenge.py <worker_id> <agent_backend> <effort> <dir> [<dir> ...]

Env:
    CLANKER_API   base URL of the running `clanker serve` (default http://127.0.0.1:8765)
"""
import io
import os
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = os.environ.get("CLANKER_API", "http://127.0.0.1:8765")


def tar_dir(d: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in sorted(d.rglob("*")):
            if p.is_file():
                tf.add(p, arcname=str(p.relative_to(d)))  # contents at top level
    return buf.getvalue()


def read(d: Path, name: str) -> str:
    f = d / name
    return f.read_text().strip() if f.is_file() else ""


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 2
    worker, agent, effort = sys.argv[1], sys.argv[2], sys.argv[3]
    for d in sys.argv[4:]:
        dp = Path(d)
        q = urllib.parse.urlencode({
            "name": dp.name, "agent_backend": agent,
            "description": read(dp, "description.txt"),
            "ideas": read(dp, "ideas.txt"),
            "flag_format": "flag{...}",
            "reasoning_effort": effort,
        })
        url = f"{BASE}/api/v1/workers/{urllib.parse.quote(worker)}/challenges?{q}"
        req = urllib.request.Request(url, data=tar_dir(dp), method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                print(f"  + {agent:<12} {dp.name:<14} -> {r.status} {r.read()[:200].decode()}")
        except urllib.error.HTTPError as e:
            print(f"  ! {agent:<12} {dp.name:<14} -> HTTP {e.code} {e.read()[:300].decode()}")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {agent:<12} {dp.name:<14} -> ERROR {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
