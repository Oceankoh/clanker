"""Golden-image metadata: which pre-baked VM image to boot, and when it was built.

The golden image bundles the toolbox packages + agent CLIs so a VM boots ready to
run without an `apt`/`npm`/`docker pull` at startup. It carries **no credentials** —
those are injected per challenge at launch (see clanker/agents/). Built once by
``images/golden/bake.sh``; this module records and reports the result so
``clanker image status`` can show the age + the rebuild command.

See docs/PROPOSAL_BOOT_WORKERS_UPLOADS.md §1.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import STATE_DIR
from .identity import normalize_provider

GOLDEN_IMAGE_PATH = STATE_DIR / "golden-image.json"
STALE_AFTER_DAYS = 30
KNOWN_PROVIDERS = ("digitalocean", "gcp")


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def load_golden_images(path: Path | None = None) -> dict:
    """All recorded golden images, keyed by normalized provider. {} if none."""
    try:
        data = json.loads(Path(path or GOLDEN_IMAGE_PATH).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_golden_image(provider: str, *, path: Path | None = None) -> dict:
    """The recorded image for one provider, or {} if not built."""
    entry = load_golden_images(path).get(normalize_provider(provider))
    return dict(entry) if isinstance(entry, dict) else {}


def record_golden_image(
    provider: str,
    *,
    image_id: str,
    image_name: str = "",
    built_at: str = "",
    agent_versions: dict | None = None,
    path: Path | None = None,
) -> dict:
    """Persist (and return) the metadata for a freshly baked image. Atomic write."""
    prov = normalize_provider(provider)
    data = load_golden_images(path)
    entry = {
        "image_id": str(image_id or ""),
        "image_name": str(image_name or ""),
        "built_at": str(built_at or "").strip() or _iso_now(),
        "agent_versions": {str(k): str(v) for k, v in (agent_versions or {}).items() if v},
    }
    data[prov] = entry
    p = Path(path or GOLDEN_IMAGE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)
    return entry


def age_days(built_at: str, *, now: datetime | None = None) -> float | None:
    dt = _parse_iso(built_at)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max((now - dt).total_seconds() / 86400.0, 0.0)


def is_stale(built_at: str, *, now: datetime | None = None,
             stale_after_days: int = STALE_AFTER_DAYS) -> bool:
    d = age_days(built_at, now=now)
    return d is not None and d >= stale_after_days


def human_age(built_at: str, *, now: datetime | None = None) -> str:
    """Coarse 'X ago' label for `clanker image status`."""
    dt = _parse_iso(built_at)
    if dt is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    secs = max((now - dt).total_seconds(), 0.0)
    if secs < 90:
        return "just now"
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        n = int(secs // size)
        if n >= 1:
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"
