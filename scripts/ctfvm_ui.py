#!/usr/bin/env python3
import argparse
import base64
import json
import mimetypes
import re
import shlex
import subprocess
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".ctfvm" / "current-run.json"
RUNS_DIR = ROOT / ".ctfvm" / "runs"
MAX_ARTIFACT_PREVIEW_BYTES = 2_000_000
MAX_PANE_TAIL_BYTES = 30_000
MAX_FINDINGS_TAIL_BYTES = 24_000
MAX_INJECT_TAIL_BYTES = 12_000
DISCOVERY_TTL_SECONDS = 15
_DISCOVERY_CACHE = {"at": 0.0, "runs": []}


def run_cmd(cmd, timeout=20):
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            cwd=str(ROOT),
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _state_valid(state):
    if not isinstance(state, dict):
        return False
    return bool(state.get("instance") and state.get("zone") and state.get("project"))


def _project_from_gcloud_config():
    rc, out, _ = run_cmd(["gcloud", "config", "get-value", "project"], timeout=5)
    if rc != 0:
        return ""
    val = out.strip()
    if val in {"", "(unset)"}:
        return ""
    return val


def _run_id_from_instance_name(name):
    instance = _normalize_instance_name(name)
    m = re.match(r"^ctfvm-(?:[A-Za-z0-9-]+-)?(\d{8}-\d{6})$", instance)
    if m:
        return m.group(1)
    m = re.search(r"(\d{8}-\d{6})$", instance)
    if m and instance.startswith("ctfvm-"):
        return m.group(1)
    return instance


def _iso_started_at_from_run_id(run_id):
    try:
        dt = datetime.strptime(run_id, "%Y%m%d-%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def discover_runs(force=False):
    now = time.monotonic()
    if (
        not force
        and _DISCOVERY_CACHE["runs"]
        and (now - _DISCOVERY_CACHE["at"] < DISCOVERY_TTL_SECONDS)
    ):
        return list(_DISCOVERY_CACHE["runs"])

    project = _project_from_gcloud_config()
    if not project:
        _DISCOVERY_CACHE["at"] = now
        _DISCOVERY_CACHE["runs"] = []
        return []

    rc, out, _ = run_cmd(
        [
            "gcloud",
            "compute",
            "instances",
            "list",
            "--project",
            project,
            "--filter=name~^ctfvm- AND status=RUNNING",
            "--format=json(name,zone,networkInterfaces[0].accessConfigs[0].natIP)",
        ],
        timeout=12,
    )
    if rc != 0:
        _DISCOVERY_CACHE["at"] = now
        _DISCOVERY_CACHE["runs"] = []
        return []

    try:
        rows = json.loads(out or "[]")
    except Exception:
        rows = []

    discovered = []
    for row in rows:
        name = str((row or {}).get("name", "")).strip()
        if not name:
            continue
        zone_raw = str((row or {}).get("zone", "")).strip()
        zone = zone_raw.rsplit("/", 1)[-1] if "/" in zone_raw else zone_raw
        run_id = _run_id_from_instance_name(name)
        discovered.append(
            {
                "run_id": run_id,
                "instance": name,
                "zone": zone,
                "project": project,
                "ip": (
                    ((row or {}).get("networkInterfaces") or [{}])[0]
                    .get("accessConfigs", [{}])[0]
                    .get("natIP", "")
                ),
                "started_at": _iso_started_at_from_run_id(run_id),
            }
        )

    _DISCOVERY_CACHE["at"] = now
    _DISCOVERY_CACHE["runs"] = discovered
    return list(discovered)


def load_state(run_id=None):
    if run_id:
        path = RUNS_DIR / f"{run_id}.json"
        if not path.exists():
            return None
        state = _load_json(path)
        return state if _state_valid(state) else None

    if not STATE_FILE.exists():
        return None
    state = _load_json(STATE_FILE)
    return state if _state_valid(state) else None


def _normalize_instance_name(name):
    s = str(name or "").strip()
    if not s:
        return ""
    s = s.lstrip("/").strip()
    if "/" in s:
        s = s.split("/")[-1].strip()
    return s


def _normalize_run_id(run_id, instance):
    rid = str(run_id or "").strip()
    if re.match(r"^\d{8}-\d{6}$", rid):
        return rid
    inferred = _run_id_from_instance_name(instance)
    if re.match(r"^\d{8}-\d{6}$", inferred):
        return inferred
    return rid


def _normalize_run_entry(entry):
    row = dict(entry or {})
    row["instance"] = _normalize_instance_name(row.get("instance", ""))
    row["run_id"] = _normalize_run_id(row.get("run_id", ""), row.get("instance", ""))
    row["zone"] = str(row.get("zone", "") or "").strip()
    row["project"] = str(row.get("project", "") or "").strip()
    row["ip"] = str(row.get("ip", "") or "").strip()
    row["started_at"] = str(row.get("started_at", "") or "").strip()
    return row


def _run_identity_keys(run):
    run = _normalize_run_entry(run)
    keys = []
    project = str((run or {}).get("project", "")).strip()
    zone = str((run or {}).get("zone", "")).strip()
    ip = str((run or {}).get("ip", "")).strip()
    instance = str((run or {}).get("instance", "")).strip()
    run_id = str((run or {}).get("run_id", "")).strip()
    if project and zone and ip:
        keys.append(f"ip:{project}|{zone}|{ip}")
    if project and zone and instance:
        keys.append(f"inst:{project}|{zone}|{instance}")
    if run_id:
        keys.append(f"run:{run_id}")
    return keys


def _merge_run_entries(old, new):
    merged = _normalize_run_entry(old)
    new = _normalize_run_entry(new)
    source_rank = {"local": 0, "current": 1, "discovered": 2}
    old_src = str((old or {}).get("__source", "local"))
    new_src = str((new or {}).get("__source", "local"))
    prefer_new = source_rank.get(new_src, 0) >= source_rank.get(old_src, 0)

    fields = ["run_id", "instance", "zone", "project", "ip", "started_at"]
    for f in fields:
        old_val = str((old or {}).get(f, "") or "")
        new_val = str((new or {}).get(f, "") or "")
        if prefer_new and new_val:
            merged[f] = new_val
        elif not old_val and new_val:
            merged[f] = new_val
    merged["__source"] = new_src if prefer_new else old_src
    return merged


def list_runs():
    runs = []
    current = load_state()
    current_run_id = (current or {}).get("run_id", "")

    if RUNS_DIR.exists():
        for p in sorted(RUNS_DIR.glob("*.json")):
            state = _load_json(p)
            if not _state_valid(state):
                continue
            run_id = str(state.get("run_id") or p.stem)
            runs.append(
                _normalize_run_entry(
                    {
                    "run_id": run_id,
                    "instance": state.get("instance", ""),
                    "zone": state.get("zone", ""),
                    "project": state.get("project", ""),
                    "ip": state.get("ip", ""),
                    "started_at": state.get("started_at", ""),
                    "__source": "local",
                    }
                )
            )

    if current and current_run_id:
        runs.append(
            _normalize_run_entry(
                {
                "run_id": current_run_id,
                "instance": current.get("instance", ""),
                "zone": current.get("zone", ""),
                "project": current.get("project", ""),
                "ip": current.get("ip", ""),
                "started_at": current.get("started_at", ""),
                "__source": "current",
                }
            )
        )

    # Fallback: discover live ctfvm-* instances when local state is incomplete.
    for d in discover_runs():
        row = _normalize_run_entry(d)
        row["__source"] = "discovered"
        runs.append(row)

    # Merge duplicates (for example, when a VM was renamed and local JSON is stale).
    merged = []
    key_to_idx = {}
    for run in runs:
        keys = _run_identity_keys(run)
        idx = None
        for k in keys:
            if k in key_to_idx:
                idx = key_to_idx[k]
                break
        if idx is None:
            idx = len(merged)
            merged.append(run)
        else:
            merged[idx] = _merge_run_entries(merged[idx], run)
        for k in _run_identity_keys(merged[idx]):
            key_to_idx[k] = idx

    runs = merged
    for r in runs:
        r.pop("__source", None)

    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs, current_run_id


def _state_from_run_entry(entry):
    if not entry:
        return None
    state = {
        "run_id": str(entry.get("run_id", "")),
        "instance": str(entry.get("instance", "")),
        "zone": str(entry.get("zone", "")),
        "project": str(entry.get("project", "")),
        "ip": str(entry.get("ip", "")),
        "remote_run_dir": "/home/ctf/run",
    }
    return state if _state_valid(state) else None


def resolve_state(run_id=None):
    wanted = str(run_id or "").strip()
    if not wanted:
        return None

    state = load_state(wanted)
    if state:
        return state

    runs, _ = list_runs()
    for r in runs:
        if str(r.get("run_id", "")) == wanted:
            return _state_from_run_entry(r)
    return None


def ssh_cmd(state, remote_cmd, timeout=20):
    cmd = [
        "gcloud",
        "compute",
        "ssh",
        state["instance"],
        "--zone",
        state["zone"],
        "--project",
        state["project"],
        "--command",
        remote_cmd,
    ]
    return run_cmd(cmd, timeout=timeout)


def get_snapshot(run_id=None):
    runs, current_run_id = list_runs()
    requested = str(run_id or "").strip()
    if not requested:
        requested = str(current_run_id or "").strip()
    if not requested:
        return {
            "error": "No run selected. Choose a run from the dropdown.",
            "runs": runs,
            "current_run_id": current_run_id,
            "run_id": "",
            "instance": "",
            "zone": "",
            "project": "",
            "ip": "",
            "windows": "",
            "windows_list": [],
            "findings_tail": "",
            "inject_tail": "",
            "artifacts": "",
            "status": "",
        }

    state = resolve_state(requested)
    if not state:
        return {
            "error": f"Run not found: {requested}",
            "runs": runs,
            "current_run_id": current_run_id,
            "run_id": requested,
        }

    snapshot = {
        "run_id": state.get("run_id", ""),
        "instance": state.get("instance", ""),
        "zone": state.get("zone", ""),
        "project": state.get("project", ""),
        "ip": state.get("ip", ""),
        "windows": "",
        "windows_list": [],
        "findings_tail": "",
        "inject_tail": "",
        "artifacts": "",
        "status": "",
        "runs": runs,
        "current_run_id": current_run_id,
    }

    rc, out, _ = run_cmd(
        [
            "gcloud",
            "compute",
            "instances",
            "describe",
            state["instance"],
            "--zone",
            state["zone"],
            "--project",
            state["project"],
            "--format=get(status)",
        ],
        timeout=15,
    )
    snapshot["status"] = out.strip() if rc == 0 else "UNKNOWN"

    remote = f"""sudo -u ctf bash -lc '
if ! tmux list-sessions >/dev/null 2>&1; then
  echo "__NO_TMUX__"
  exit 0
fi
echo "__WINDOWS_BEGIN__"
tmux list-sessions -F "#{{session_name}}" 2>/dev/null | while IFS= read -r sess; do
  [ -z "$sess" ] && continue
  tmux list-windows -t "$sess" -F "#{{window_index}}|#{{window_name}}|#{{window_active}}" 2>/dev/null | while IFS="|" read -r idx wname wactive; do
    printf "%s|%s|%s|%s\\n" "$sess" "$idx" "$wname" "$wactive"
  done
done
echo "__WINDOWS_END__"
tmux list-sessions -F "#{{session_name}}" 2>/dev/null | while IFS= read -r sess; do
  [ -z "$sess" ] && continue
  tmux list-windows -t "$sess" -F "#{{window_index}}" 2>/dev/null | while IFS= read -r idx; do
    [ -z "$idx" ] && continue
    printf "__PANE_BEGIN__%s|%s\\n" "$sess" "$idx"
    tmux capture-pane -p -t "${{sess}}:${{idx}}" -S -120 2>/dev/null | tail -c {MAX_PANE_TAIL_BYTES} || echo "window ${{sess}}:${{idx}} not found"
    printf "__PANE_END__%s|%s\\n" "$sess" "$idx"
  done
done
echo "__INJECT__"
tail -c {MAX_INJECT_TAIL_BYTES} /home/ctf/run/inject.queue 2>/dev/null || true
echo "__FINDINGS__"
tail -c {MAX_FINDINGS_TAIL_BYTES} /home/ctf/run/findings.md 2>/dev/null || true
echo "__ART__"
find /home/ctf/run/artifacts -maxdepth 3 -type f 2>/dev/null | sed "s#^/home/ctf/run/##" | sort || true
'"""
    rc, out, err = ssh_cmd(state, remote, timeout=25)
    if rc != 0:
        snapshot["error"] = (err or out or "Failed to query VM").strip()
        return snapshot

    if "__NO_TMUX__" in out:
        snapshot["error"] = "No tmux sessions found in VM."
        return snapshot

    def slice_between(text, start_marker, end_marker):
        s = text.find(start_marker)
        if s == -1:
            return ""
        s += len(start_marker)
        e = text.find(end_marker, s)
        if e == -1:
            e = len(text)
        return text[s:e].strip()

    windows_block = slice_between(out, "__WINDOWS_BEGIN__", "__WINDOWS_END__")
    if not windows_block:
        snapshot["error"] = "Unexpected monitor output format."
        return snapshot

    windows_list = []
    for line in windows_block.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        session = parts[0].strip()
        idx = parts[1].strip()
        name = parts[2].strip()
        active = parts[3].strip() == "1"
        output = slice_between(
            out,
            f"__PANE_BEGIN__{session}|{idx}",
            f"__PANE_END__{session}|{idx}",
        )
        windows_list.append(
            {
                "session": session,
                "index": idx,
                "name": name,
                "active": active,
                "target": f"{session}:{idx}",
                "output": output,
            }
        )

    snapshot["windows_list"] = windows_list
    snapshot["windows"] = "\n".join(
        f"{w['target']}: {w['name']}{' *' if w['active'] else ''}" for w in windows_list
    )
    snapshot["inject_tail"] = slice_between(out, "__INJECT__", "__FINDINGS__")
    snapshot["findings_tail"] = slice_between(out, "__FINDINGS__", "__ART__")
    snapshot["artifacts"] = out.split("__ART__", 1)[1].strip() if "__ART__" in out else ""
    return snapshot


def _sanitize_artifact_relpath(relpath):
    relpath = (relpath or "").strip().replace("\\", "/")
    if not relpath:
        return None
    if relpath.startswith("/") or relpath.startswith("~"):
        return None
    parts = [p for p in relpath.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        return None
    clean = "/".join(parts)
    if not clean.startswith("artifacts/"):
        return None
    return clean


def get_artifact_preview(relpath, run_id=None, max_bytes=MAX_ARTIFACT_PREVIEW_BYTES):
    state = resolve_state(run_id)
    if not state:
        return {"ok": False, "error": "No active run. Start one with ./scripts/ctfvm start ..."}

    clean = _sanitize_artifact_relpath(relpath)
    if not clean:
        return {"ok": False, "error": "Invalid artifact path"}

    encoded_path = base64.b64encode(clean.encode("utf-8")).decode("ascii")
    remote = (
        "sudo -u ctf bash -lc '"
        f"rel=$(printf %s {encoded_path} | base64 -d); "
        'full="/home/ctf/run/${rel}"; '
        'if [ ! -f "$full" ]; then echo "__ERR__not found"; exit 0; fi; '
        'mime=$(file -b --mime-type "$full" 2>/dev/null || echo application/octet-stream); '
        'size=$(wc -c < "$full" | tr -d " "); '
        'echo "__META__${mime}|${size}"; '
        f'dd if="$full" bs=1 count={max_bytes} 2>/dev/null | base64; '
        f'if [ "$size" -gt {max_bytes} ]; then echo "__TRUNC__1"; else echo "__TRUNC__0"; fi'
        "'"
    )
    rc, out, err = ssh_cmd(state, remote, timeout=25)
    if rc != 0:
        return {"ok": False, "error": (err or out or "Failed to fetch artifact").strip()}
    if out.strip().startswith("__ERR__"):
        return {"ok": False, "error": "Artifact not found"}

    lines = out.splitlines()
    if not lines or not lines[0].startswith("__META__"):
        return {"ok": False, "error": "Unexpected artifact preview format"}

    meta = lines[0][len("__META__") :]
    mime = "application/octet-stream"
    size = 0
    if "|" in meta:
        mime, size_text = meta.split("|", 1)
        try:
            size = int(size_text)
        except ValueError:
            size = 0

    trunc_line = "__TRUNC__0"
    if lines:
        last = lines[-1].strip()
        if last.startswith("__TRUNC__"):
            trunc_line = last
            lines = lines[1:-1]
        else:
            lines = lines[1:]
    truncated = trunc_line == "__TRUNC__1"

    payload_b64 = "".join(lines).strip()
    raw = b""
    if payload_b64:
        try:
            raw = base64.b64decode(payload_b64, validate=False)
        except Exception:
            raw = b""

    guessed, _ = mimetypes.guess_type(clean)
    effective_mime = mime if mime and mime != "application/octet-stream" else (guessed or mime)
    text_like = effective_mime.startswith("text/") or effective_mime in {
        "application/json",
        "application/xml",
        "application/x-sh",
        "application/javascript",
    }

    is_image = effective_mime.startswith("image/")
    image_data_url = ""
    if is_image and raw:
        image_data_url = f"data:{effective_mime};base64,{base64.b64encode(raw).decode('ascii')}"

    if text_like:
        content = raw.decode("utf-8", errors="replace")
    else:
        content = f"[binary file: {effective_mime}, {size} bytes]"

    return {
        "ok": True,
        "path": clean,
        "mime": effective_mime,
        "size": size,
        "truncated": truncated,
        "is_text": text_like,
        "is_image": is_image,
        "image_data_url": image_data_url,
        "content": content,
    }


_TARGET_RE = re.compile(r"^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$")
_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _state_for_run_or_error(run_id):
    requested = str(run_id or "").strip()
    if not requested:
        _, current_run_id = list_runs()
        requested = str(current_run_id or "").strip()
    if not requested:
        return None, {"ok": False, "error": "No run selected"}
    state = resolve_state(requested)
    if not state:
        return None, {"ok": False, "error": f"Run not found: {requested}"}
    return state, None


def _safe_target(target):
    target = (target or "ctf:supervisor").strip() or "ctf:supervisor"
    if not _TARGET_RE.match(target):
        return None
    return target


def _target_session(target):
    if ":" not in target:
        return "ctf"
    return target.split(":", 1)[0]


def send_to_tmux(run_id, target, text, enter=True):
    state, err = _state_for_run_or_error(run_id)
    if err:
        return err
    safe_target = _safe_target(target)
    if not safe_target:
        return {"ok": False, "error": "Invalid tmux target"}
    safe_session = _target_session(safe_target)
    if not text:
        return {"ok": False, "error": "text is required"}

    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    # Send literal text first, then submit with C-m after a brief delay.
    enter_cmd = (
        "sleep 0.08; tmux send-keys -t {} C-m;".format(shlex.quote(safe_target)) if enter else ""
    )
    remote = (
        "sudo -u ctf bash -lc '"
        f"tmux has-session -t {shlex.quote(safe_session)} >/dev/null 2>&1 || exit 1; "
        f'payload="$(printf %s {shlex.quote(payload)} | base64 -d)"; '
        f"tmux send-keys -t {shlex.quote(safe_target)} -l -- \"${{payload}}\"; "
        f"{enter_cmd}"
        "'"
    )
    rc, out, stderr = ssh_cmd(state, remote, timeout=20)
    if rc != 0:
        return {"ok": False, "error": (stderr or out or "send failed").strip()}
    return {"ok": True}


def send_keys_tmux(run_id, target, keys):
    state, err = _state_for_run_or_error(run_id)
    if err:
        return err
    safe_target = _safe_target(target)
    if not safe_target:
        return {"ok": False, "error": "Invalid tmux target"}
    safe_session = _target_session(safe_target)
    safe_keys = [k for k in keys if _KEY_RE.match(k or "")]
    if not safe_keys:
        return {"ok": False, "error": "No valid keys provided"}
    keys_cmd = " ".join(shlex.quote(k) for k in safe_keys)
    remote = (
        "sudo -u ctf bash -lc '"
        f"tmux has-session -t {shlex.quote(safe_session)} >/dev/null 2>&1 || exit 1; "
        f"tmux send-keys -t {shlex.quote(safe_target)} {keys_cmd}'"
    )
    rc, out, stderr = ssh_cmd(state, remote, timeout=15)
    if rc != 0:
        return {"ok": False, "error": (stderr or out or "key failed").strip()}
    return {"ok": True}


def inject_guidance(run_id, msg):
    state, err = _state_for_run_or_error(run_id)
    if err:
        return err
    if not msg:
        return {"ok": False, "error": "msg is required"}
    line = f"[{datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    payload = base64.b64encode(line.encode("utf-8")).decode("ascii")
    remote = (
        "sudo -u ctf bash -lc '"
        f"printf %s {shlex.quote(payload)} | base64 -d >> /home/ctf/run/inject.queue; "
        'printf "\\n" >> /home/ctf/run/inject.queue\''
    )
    rc, out, stderr = ssh_cmd(state, remote, timeout=15)
    if rc != 0:
        return {"ok": False, "error": (stderr or out or "inject failed").strip()}
    return {"ok": True}


def trust_prompt(run_id, target="ctf:supervisor"):
    return send_keys_tmux(run_id, target, ["1", "Enter"])


INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ctfvm ui</title>
  <style>
    :root { --bg: #0f1218; --card: #171b24; --line: #2a3242; --text: #e9eefb; --muted: #a8b3c9; --acc: #7bd88f; }
    body { margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: var(--bg); color: var(--text); }
    .wrap { padding: 12px; display: grid; gap: 10px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .panes-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 10px; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; min-height: 120px; }
    .card.selected-pane { border-color: #7bd88f; box-shadow: 0 0 0 1px rgba(123,216,143,0.45), 0 0 18px rgba(123,216,143,0.18); }
    .card.pane-collapsed { display: none; }
    .head { padding: 8px 10px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; }
    .pane-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .pane-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pane-toggle { font-size: 11px; padding: 3px 8px; }
    .closed-panes { display: flex; gap: 6px; flex-wrap: wrap; padding: 8px 10px; border-bottom: 1px solid var(--line); }
    .closed-panes:empty { display: none; }
    .chip-btn { font-size: 11px; padding: 3px 8px; }
    pre { margin: 0; padding: 10px; white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 40vh; overflow: auto; }
    * { box-sizing: border-box; }
    .top { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    input, button, select { background: #0c1017; color: var(--text); border: 1px solid var(--line); padding: 6px 8px; border-radius: 6px; font-family: inherit; font-size: 13px; line-height: 1.2; }
    button { cursor: pointer; }
    select { appearance: none; -webkit-appearance: none; -moz-appearance: none; padding-right: 26px; }
    #targetSelect {
      min-width: 260px;
      background-image:
        linear-gradient(45deg, transparent 50%, #a8b3c9 50%),
        linear-gradient(135deg, #a8b3c9 50%, transparent 50%);
      background-position:
        calc(100% - 14px) calc(50% - 2px),
        calc(100% - 9px) calc(50% - 2px);
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
    }
    .ok { color: var(--acc); }
    .warn { color: #ff7a7a; }
    .control-block { background: #121926; border: 1px solid var(--line); border-radius: 8px; padding: 10px; margin-top: 8px; }
    .control-title { font-size: 12px; color: var(--muted); margin-bottom: 8px; letter-spacing: 0.2px; }
    .status { font-size: 12px; color: var(--muted); min-width: 0; white-space: nowrap; }
    .status:empty { display: none; }
    .action-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 8px; width: 100%; }
    .text-input { flex: 1 1 420px; min-width: 0; }
    .action-btn { min-width: 84px; height: 34px; padding: 6px 12px; font-size: 13px; line-height: 1.1; }
    @media (max-width: 1100px) { .row { grid-template-columns: 1fr; } pre { max-height: 28vh; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="head">Controls</div>
      <div style="padding:10px">
        <div class="top">
          <span id="meta">loading...</span>
          <label style="font-size:12px;color:#a8b3c9">run:
            <select id="runSelect" onchange="onRunChange()" style="width:220px;"></select>
          </label>
          <button onclick="refreshNow()">Refresh</button>
          <button onclick="collapseAllPanes()">Hide all pane outputs</button>
          <button onclick="expandAllPanes()">Show all pane outputs</button>
          <button onclick="acceptTrust()">Accept trust prompt</button>
          <button onclick="sendCtrlC()">Interrupt (Ctrl-C)</button>
          <button onclick="submitEnter()">Submit (Enter)</button>
        </div>
        <div class="control-block">
          <div class="control-title">Direct Message (immediate input to selected tmux pane)</div>
          <div class="action-row">
            <label style="font-size:12px;color:#a8b3c9">send to:
              <select id="targetSelect" onchange="onTargetModeChange()" style="width:240px;"></select>
            </label>
            <input id="targetCustom" value="ctf:supervisor" style="width:180px;display:none;" placeholder="custom tmux target" />
          </div>
          <div class="action-row">
            <input id="chatmsg" class="text-input" placeholder="Send message to Codex..."
                   onkeydown="if(event.key==='Enter' && !event.shiftKey){event.preventDefault();sendChat();}" />
            <button class="action-btn" onclick="sendChat()">Send</button>
            <span id="sendStatus" class="status"></span>
          </div>
        </div>
        <div class="control-block">
          <div class="control-title">Inject Guidance (appends to inject queue)</div>
          <div class="action-row">
            <input id="inject" class="text-input" placeholder="Inject guidance..."
                   onkeydown="if(event.key==='Enter' && !event.shiftKey){event.preventDefault();inject();}" />
            <button class="action-btn" onclick="inject()">Inject</button>
            <span id="injectStatus" class="status"></span>
          </div>
        </div>
      </div>
    </div>

    <div class="card"><div class="head">tmux windows</div><pre id="windows"></pre></div>

    <div class="card">
      <div class="head">tmux pane outputs (dynamic)</div>
      <div id="closedPanes" class="closed-panes"></div>
      <div id="panes" class="panes-grid" style="padding:10px;"></div>
    </div>

    <div class="row">
      <div class="card"><div class="head">inject queue (tail)</div><pre id="inject_tail"></pre></div>
      <div class="card"><div class="head">findings.md (tail)</div><pre id="findings_tail"></pre></div>
    </div>
    <div class="row">
      <div class="card">
        <div class="head">artifacts (click to preview)</div>
        <div id="artifactsList" style="padding:10px;max-height:40vh;overflow:auto;"></div>
      </div>
      <div class="card">
        <div class="head">artifact preview <span id="artifactMeta" style="float:right;"></span></div>
        <div style="padding:10px;" id="artifactImageWrap">
          <img id="artifactImage" alt="artifact preview" style="display:none;max-width:100%;max-height:48vh;border:1px solid var(--line);border-radius:6px;" />
        </div>
        <pre id="artifactPreview"></pre>
      </div>
    </div>
  </div>
<script>
  let selectedArtifact = null;
  let selectedRunId = '';
  let refreshInFlight = false;
  let refreshCounter = 0;
  const followState = {};
  const paneCollapsedState = {};
  let latestWindowsList = [];

  function isNearBottom(el, threshold = 24) {
    if (!el) return true;
    return (el.scrollTop + el.clientHeight) >= (el.scrollHeight - threshold);
  }

  function bindFollowTracking(el, key) {
    if (!el || !key) return;
    if (!el.dataset.followBound) {
      el.addEventListener('scroll', () => {
        followState[key] = isNearBottom(el);
      }, { passive: true });
      el.dataset.followBound = '1';
    }
  }

  function setStatus(id, msg) {
    const el = document.getElementById(id);
    if (el) el.textContent = msg || '';
  }

  function paneCollapsed(target) {
    return paneCollapsedState[target] === true;
  }

  function applyPaneCollapsedUI(card, target, toggleBtn) {
    const collapsed = paneCollapsed(target);
    if (card) card.classList.toggle('pane-collapsed', collapsed);
    if (toggleBtn) {
      toggleBtn.textContent = collapsed ? 'open' : 'close';
      toggleBtn.title = collapsed ? 'Reopen pane output' : 'Hide pane output';
    }
  }

  function setPaneCollapsed(target, collapsed) {
    if (!target) return;
    paneCollapsedState[target] = !!collapsed;
    const card = document.getElementById(paneCardIdFromTarget(target));
    if (card) {
      const toggleBtn = card.querySelector('.pane-toggle');
      applyPaneCollapsedUI(card, target, toggleBtn);
    }
    renderClosedPanes(latestWindowsList);
  }

  function collapseAllPanes() {
    const panes = document.getElementById('panes');
    if (!panes) return;
    panes.querySelectorAll('[data-pane-card="1"]').forEach(card => {
      const target = card.dataset.paneTarget || '';
      if (target) setPaneCollapsed(target, true);
    });
    renderClosedPanes(latestWindowsList);
  }

  function expandAllPanes() {
    const panes = document.getElementById('panes');
    if (!panes) return;
    panes.querySelectorAll('[data-pane-card="1"]').forEach(card => {
      const target = card.dataset.paneTarget || '';
      if (target) setPaneCollapsed(target, false);
    });
    renderClosedPanes(latestWindowsList);
  }

  function renderClosedPanes(windowsList) {
    const holder = document.getElementById('closedPanes');
    if (!holder) return;
    holder.innerHTML = '';
    const list = Array.isArray(windowsList) ? windowsList : [];
    const closed = list.filter(w => paneCollapsed(w.target || `${w.session}:${w.index}`));
    if (!closed.length) return;

    const label = document.createElement('span');
    label.style.color = '#a8b3c9';
    label.style.fontSize = '11px';
    label.style.padding = '4px 2px';
    label.textContent = 'closed:';
    holder.appendChild(label);

    closed.forEach(w => {
      const target = w.target || `${w.session}:${w.index}`;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip-btn';
      btn.textContent = `open ${target}`;
      btn.onclick = () => setPaneCollapsed(target, false);
      holder.appendChild(btn);
    });
  }

  function onPaneToggleClick(ev) {
    const btn = ev.target && ev.target.closest ? ev.target.closest('.pane-toggle') : null;
    if (!btn) return;
    ev.preventDefault();
    ev.stopPropagation();
    const target = (btn.dataset.paneTarget || '').trim();
    if (!target) return;
    setPaneCollapsed(target, !paneCollapsed(target));
  }

  function selectedRunQueryParam() {
    return selectedRunId ? ('?run_id=' + encodeURIComponent(selectedRunId)) : '';
  }

  function payloadWithRun(payload) {
    const base = Object.assign({}, payload || {});
    if (selectedRunId) base.run_id = selectedRunId;
    return base;
  }

  function onRunChange() {
    const sel = document.getElementById('runSelect');
    if (!sel) return;
    selectedRunId = sel.value || '';
    selectedArtifact = null;
    refreshNow();
  }

  function syncRunOptions(runs, currentRunId, snapshotRunId) {
    const sel = document.getElementById('runSelect');
    if (!sel) return;
    const prev = selectedRunId || sel.value || '';
    sel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'select run';
    sel.appendChild(placeholder);
    const list = Array.isArray(runs) ? runs : [];
    list.forEach(run => {
      const opt = document.createElement('option');
      opt.value = run.run_id || '';
      opt.textContent = `${run.instance || 'unknown'} (${run.run_id || 'n/a'})`;
      sel.appendChild(opt);
    });
    const available = Array.from(sel.options).map(o => o.value);
    const target = [prev, snapshotRunId || '', currentRunId || ''].find(v => available.includes(v));
    sel.value = target || '';
    selectedRunId = sel.value || '';
  }

  function onTargetModeChange() {
    const sel = document.getElementById('targetSelect');
    const custom = document.getElementById('targetCustom');
    if (!sel || !custom) return;
    custom.style.display = sel.value === '__custom__' ? 'inline-block' : 'none';
    highlightSelectedPane();
  }

  function paneCardIdFromTarget(target) {
    const safe = String(target || '').replace(/[^A-Za-z0-9_.-]/g, '_');
    return `pane-card-${safe}`;
  }

  function selectedPaneTargetFromTarget() {
    const sel = document.getElementById('targetSelect');
    const custom = document.getElementById('targetCustom');
    if (!sel) return null;
    let target = sel.value || '';
    if (target === '__custom__') {
      target = (custom && custom.value || '').trim();
    }
    const m = /^[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+$/.exec(target);
    return m ? target : null;
  }

  function highlightSelectedPane() {
    const panes = document.getElementById('panes');
    if (!panes) return;
    panes.querySelectorAll('[data-pane-card="1"]').forEach(card => card.classList.remove('selected-pane'));
    const target = selectedPaneTargetFromTarget();
    if (target === null) return;
    const card = document.getElementById(paneCardIdFromTarget(target));
    if (card) card.classList.add('selected-pane');
  }

  function syncTargetOptions(windowsList) {
    const sel = document.getElementById('targetSelect');
    if (!sel) return;
    const previous = sel.value || 'ctf:supervisor';
    sel.innerHTML = '';

    const list = Array.isArray(windowsList) ? windowsList : [];
    if (list.length) {
      list.forEach(w => {
        const opt = document.createElement('option');
        opt.value = w.target || `${w.session}:${w.index}`;
        opt.textContent = `${w.active ? '* ' : ''}${w.name} (${opt.value})`;
        sel.appendChild(opt);
      });
    } else {
      const opt = document.createElement('option');
      opt.value = 'ctf:supervisor';
      opt.textContent = 'supervisor (ctf:supervisor)';
      sel.appendChild(opt);
    }

    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = 'Custom target...';
    sel.appendChild(customOpt);

    const available = Array.from(sel.options).map(o => o.value);
    sel.value = available.includes(previous) ? previous : sel.options[0].value;
    onTargetModeChange();
  }

  async function refreshNow() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    const refreshId = ++refreshCounter;
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 12000);
      const r = await fetch('/api/snapshot' + selectedRunQueryParam(), {
        cache: 'no-store',
        signal: ctl.signal
      });
      clearTimeout(timer);
      const d = await r.json();
      const meta = d.error
        ? `<span class="warn">${d.error}</span>`
        : `<span class="ok">${d.status}</span> ${d.instance} (${d.zone}) ip=${d.ip} run=${d.run_id || '-'} <span style="color:#a8b3c9">#${refreshId}</span>`;
      document.getElementById('meta').innerHTML = meta;
      syncRunOptions(d.runs || [], d.current_run_id || '', d.run_id || '');
      function stickBottom(el) {
        if (!el) return;
        el.scrollTop = el.scrollHeight;
      }
      function setPreAndStickBottom(id, value) {
        const el = document.getElementById(id);
        if (!el) return;
        const nextText = value || '';
        if (el.textContent === nextText) return;
        const key = `static:${id}`;
        bindFollowTracking(el, key);
        const follow = followState[key] !== false ? isNearBottom(el) : followState[key];
        const prevTop = el.scrollTop;
        el.textContent = nextText;
        requestAnimationFrame(() => {
          if (follow) {
            stickBottom(el);
          } else {
            el.scrollTop = prevTop;
          }
        });
      }
      ['windows','inject_tail','findings_tail'].forEach(k => {
        setPreAndStickBottom(k, d[k]);
      });
      syncTargetOptions(d.windows_list || []);
      renderArtifactsList(d.artifacts || '');

      const panes = document.getElementById('panes');
      if (panes && !panes.dataset.toggleBound) {
        panes.addEventListener('click', onPaneToggleClick);
        panes.dataset.toggleBound = '1';
      }
      const list = d.windows_list || [];
      latestWindowsList = Array.isArray(list) ? list : [];
      renderClosedPanes(latestWindowsList);
      const emptyId = 'panes-empty-card';
      const expectedIds = new Set(list.map(w => paneCardIdFromTarget(w.target || `${w.session}:${w.index}`)));

      panes.querySelectorAll('[data-pane-card="1"]').forEach(card => {
        if (!expectedIds.has(card.id)) {
          const oldTarget = card.dataset.paneTarget || '';
          if (oldTarget) delete paneCollapsedState[oldTarget];
          card.remove();
        }
      });

      if (!list.length) {
        if (!document.getElementById(emptyId)) {
          const empty = document.createElement('div');
          empty.className = 'card';
          empty.id = emptyId;
          empty.innerHTML = '<div class="head">no tmux windows</div><pre></pre>';
          panes.appendChild(empty);
        }
        return;
      }

      const existingEmpty = document.getElementById(emptyId);
      if (existingEmpty) existingEmpty.remove();

      list.forEach(w => {
        const target = w.target || `${w.session}:${w.index}`;
        const paneKey = `pane:${target}`;
        const cardId = paneCardIdFromTarget(target);
        let card = document.getElementById(cardId);
        let head;
        let pre;

        if (!card) {
          card = document.createElement('div');
          card.className = 'card';
          card.id = cardId;
          card.dataset.paneCard = '1';
          card.dataset.paneTarget = target;

          head = document.createElement('div');
          head.className = 'head pane-head';

          const title = document.createElement('span');
          title.className = 'pane-title';
          head.appendChild(title);

          const toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'pane-toggle';
          head.appendChild(toggleBtn);

          card.appendChild(head);

          pre = document.createElement('pre');
          pre.dataset.paneKey = paneKey;
          bindFollowTracking(pre, paneKey);
          card.appendChild(pre);
          panes.appendChild(card);
        } else {
          head = card.querySelector('.head');
          pre = card.querySelector('pre');
          card.dataset.paneTarget = target;
          head.classList.add('pane-head');
          if (!pre.dataset.paneKey) pre.dataset.paneKey = paneKey;
          bindFollowTracking(pre, paneKey);
        }

        let title = head.querySelector('.pane-title');
        if (!title) {
          title = document.createElement('span');
          title.className = 'pane-title';
          head.textContent = '';
          head.appendChild(title);
        }
        let toggleBtn = head.querySelector('.pane-toggle');
        if (!toggleBtn) {
          toggleBtn = document.createElement('button');
          toggleBtn.type = 'button';
          toggleBtn.className = 'pane-toggle';
          head.appendChild(toggleBtn);
        }
        toggleBtn.dataset.paneTarget = target;
        title.textContent = `${w.active ? '* ' : ''}${target}: ${w.name}`;
        applyPaneCollapsedUI(card, target, toggleBtn);

        const nextOutput = w.output || '';
        if (pre.textContent !== nextOutput) {
          const follow = followState[paneKey] !== false ? isNearBottom(pre) : followState[paneKey];
          const prevTop = pre.scrollTop;
          pre.textContent = nextOutput;
          requestAnimationFrame(() => {
            if (follow) {
              stickBottom(pre);
            } else {
              pre.scrollTop = prevTop;
            }
          });
        }
      });
      highlightSelectedPane();
    } catch (e) {
      const meta = document.getElementById('meta');
      if (meta) meta.innerHTML = `<span class="warn">UI refresh failed: ${String(e)}</span>`;
    } finally {
      refreshInFlight = false;
    }
  }

  async function inject() {
    if (!selectedRunId) {
      setStatus('injectStatus', 'select run first');
      return;
    }
    const input = document.getElementById('inject');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    setStatus('injectStatus', 'injecting...');
    try {
      const r = await fetch('/api/inject', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payloadWithRun({msg}))
      });
      const d = await r.json();
      setStatus('injectStatus', d.ok ? 'injected' : ('error: ' + (d.error || 'failed')));
    } catch (e) {
      setStatus('injectStatus', 'error: send failed');
      input.value = msg;
    }
    refreshNow();
  }

  async function acceptTrust() {
    if (!selectedRunId) {
      setStatus('sendStatus', 'select run first');
      return;
    }
    const targetSel = document.getElementById('targetSelect');
    const targetCustom = document.getElementById('targetCustom');
    const target = (targetSel && targetSel.value === '__custom__'
      ? (targetCustom.value.trim() || 'ctf:supervisor')
      : ((targetSel && targetSel.value) || 'ctf:supervisor'));
    const r = await fetch('/api/trust', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payloadWithRun({target}))
    });
    const d = await r.json();
    setStatus('sendStatus', d.ok ? 'trust accepted' : ('error: ' + (d.error || 'failed')));
    await refreshNow();
  }

  async function sendChat() {
    if (!selectedRunId) {
      setStatus('sendStatus', 'select run first');
      return;
    }
    const input = document.getElementById('chatmsg');
    const text = input.value.trim();
    const targetSel = document.getElementById('targetSelect');
    const targetCustom = document.getElementById('targetCustom');
    const target = (targetSel && targetSel.value === '__custom__'
      ? (targetCustom.value.trim() || 'ctf:supervisor')
      : ((targetSel && targetSel.value) || 'ctf:supervisor'));
    if (!text) return;
    input.value = '';
    setStatus('sendStatus', `sending to ${target}...`);
    try {
      const r = await fetch('/api/send', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify(payloadWithRun({text, target, enter: true}))
      });
      const d = await r.json();
      setStatus('sendStatus', d.ok ? `sent to ${target}` : ('error: ' + (d.error || 'failed')));
    } catch (e) {
      setStatus('sendStatus', 'error: send failed');
      input.value = text;
    }
    refreshNow();
  }

  async function sendCtrlC() {
    if (!selectedRunId) {
      setStatus('sendStatus', 'select run first');
      return;
    }
    const targetSel = document.getElementById('targetSelect');
    const targetCustom = document.getElementById('targetCustom');
    const target = (targetSel && targetSel.value === '__custom__'
      ? (targetCustom.value.trim() || 'ctf:supervisor')
      : ((targetSel && targetSel.value) || 'ctf:supervisor'));
    const r = await fetch('/api/key', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payloadWithRun({target, keys: ['C-c']}))
    });
    const d = await r.json();
    setStatus('sendStatus', d.ok ? 'sent Ctrl-C' : ('error: ' + (d.error || 'failed')));
    await refreshNow();
  }

  async function submitEnter() {
    if (!selectedRunId) {
      setStatus('sendStatus', 'select run first');
      return;
    }
    const targetSel = document.getElementById('targetSelect');
    const targetCustom = document.getElementById('targetCustom');
    const target = (targetSel && targetSel.value === '__custom__'
      ? (targetCustom.value.trim() || 'ctf:supervisor')
      : ((targetSel && targetSel.value) || 'ctf:supervisor'));
    const r = await fetch('/api/key', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(payloadWithRun({target, keys: ['C-m']}))
    });
    const d = await r.json();
    setStatus('sendStatus', d.ok ? 'sent Enter' : ('error: ' + (d.error || 'failed')));
    await refreshNow();
  }

  async function copyCmd(v) {
    await navigator.clipboard.writeText(v);
  }

  function renderArtifactsList(raw) {
    const holder = document.getElementById('artifactsList');
    if (!holder) return;
    holder.innerHTML = '';
    const lines = String(raw || '').split('\\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length) {
      holder.textContent = 'No artifact files yet.';
      return;
    }
    lines.forEach(path => {
      const btn = document.createElement('button');
      btn.style.display = 'block';
      btn.style.width = '100%';
      btn.style.textAlign = 'left';
      btn.style.marginBottom = '6px';
      btn.textContent = path;
      btn.onclick = async () => {
        selectedArtifact = path;
        await loadArtifactPreview(path);
      };
      holder.appendChild(btn);
    });
    if (!selectedArtifact || !lines.includes(selectedArtifact)) {
      selectedArtifact = lines[0];
      loadArtifactPreview(selectedArtifact);
    }
  }

  async function loadArtifactPreview(path) {
    const pre = document.getElementById('artifactPreview');
    const meta = document.getElementById('artifactMeta');
    if (!pre || !meta) return;
    pre.textContent = 'Loading...';
    meta.textContent = '';
    const runQuery = selectedRunId ? ('&run_id=' + encodeURIComponent(selectedRunId)) : '';
    const r = await fetch('/api/artifact?path=' + encodeURIComponent(path) + runQuery);
    const d = await r.json();
    if (!d.ok) {
      const img = document.getElementById('artifactImage');
      if (img) { img.style.display = 'none'; img.src = ''; }
      pre.textContent = 'Error: ' + (d.error || 'failed to load');
      return;
    }
    meta.textContent = `${d.mime} | ${d.size} bytes${d.truncated ? ' | truncated' : ''}`;
    const img = document.getElementById('artifactImage');
    if (d.is_image && d.image_data_url && img) {
      img.src = d.image_data_url;
      img.style.display = 'block';
      pre.style.display = 'none';
      pre.textContent = '';
    } else {
      if (img) { img.style.display = 'none'; img.src = ''; }
      pre.style.display = 'block';
      pre.textContent = d.content || '';
    }
    requestAnimationFrame(() => {
      pre.scrollTop = pre.scrollHeight;
    });
  }

  const metaEl = document.getElementById('meta');
  if (metaEl) metaEl.textContent = 'js ready; loading snapshot...';
  const chatInput = document.getElementById('chatmsg');
  if (chatInput) {
    chatInput.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendChat();
      }
    });
  }
  const targetCustom = document.getElementById('targetCustom');
  if (targetCustom) {
    targetCustom.addEventListener('input', () => {
      if ((document.getElementById('targetSelect') || {}).value === '__custom__') {
        highlightSelectedPane();
      }
    });
  }
  setInterval(refreshNow, 2500);
  refreshNow();

  window.addEventListener('error', (ev) => {
    const meta = document.getElementById('meta');
    if (meta) meta.innerHTML = `<span class="warn">UI script error: ${ev.message}</span>`;
  });
  window.addEventListener('unhandledrejection', (ev) => {
    const meta = document.getElementById('meta');
    if (meta) meta.innerHTML = `<span class="warn">UI promise error: ${String(ev.reason)}</span>`;
  });
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content):
        body = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/artifact"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query or "")
            relpath = (params.get("path") or [""])[0]
            run_id = (params.get("run_id") or [""])[0]
            preview = get_artifact_preview(relpath, run_id=run_id)
            self._send_json(preview, status=HTTPStatus.OK if preview.get("ok") else HTTPStatus.BAD_REQUEST)
            return
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(INDEX_HTML)
            return
        if self.path.startswith("/api/snapshot"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query or "")
            run_id = (params.get("run_id") or [""])[0]
            self._send_json(get_snapshot(run_id=run_id))
            return
        if self.path == "/api/runs":
            runs, current_run_id = list_runs()
            self._send_json({"runs": runs, "current_run_id": current_run_id})
            return
        self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path == "/api/send":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8") or "{}")
                text = str(payload.get("text", "")).strip()
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
                enter = bool(payload.get("enter", True))
                run_id = str(payload.get("run_id", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return

            result = send_to_tmux(run_id, target, text, enter=enter)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/key":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8") or "{}")
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
                keys = payload.get("keys", [])
                if not isinstance(keys, list):
                    raise ValueError("keys must be a list")
                keys = [str(k).strip() for k in keys if str(k).strip()]
                run_id = str(payload.get("run_id", "")).strip()
            except Exception:
                self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
                return

            result = send_keys_tmux(run_id, target, keys)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/trust":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8") or "{}")
                run_id = str(payload.get("run_id", "")).strip()
                target = str(payload.get("target", "ctf:supervisor")).strip() or "ctf:supervisor"
            except Exception:
                run_id = ""
                target = "ctf:supervisor"
            result = trust_prompt(run_id, target=target)
            self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)
            return

        if self.path != "/api/inject":
            self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8") or "{}")
            msg = str(payload.get("msg", "")).strip()
            run_id = str(payload.get("run_id", "")).strip()
        except Exception:
            self._send_json({"ok": False, "error": "invalid payload"}, status=HTTPStatus.BAD_REQUEST)
            return

        result = inject_guidance(run_id, msg)
        self._send_json(result, status=HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST)

    def log_message(self, format, *args):
        return


def main():
    parser = argparse.ArgumentParser(description="Local web UI for ctfvm runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ctfvm ui listening on http://{args.host}:{args.port}")
    print("Open in browser, then use Inject/monitor views.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
