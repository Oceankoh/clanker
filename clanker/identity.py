"""Run identity & normalization helpers.

Ported verbatim (behavior-preserving) from the current ``ctfvm_ui`` code so that
``RunRegistry`` produces exactly the same dedup/merge results as the old
``CTFVMUIService.list_runs``. These operate on plain strings / dict entries and
have no dependency on the rest of the package, so both ``models`` and ``state``
can import them freely.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Provider / instance / run-id normalization
# ---------------------------------------------------------------------------


def normalize_provider(provider: Any) -> str:
    provider = str(provider or "").strip().lower()
    if provider in {"", "gcp", "google", "google-cloud", "googlecloud"}:
        return "gcp"
    if provider in {"do", "digitalocean", "digital-ocean"}:
        return "digitalocean"
    return provider


def provider_from_state(state: dict | None) -> str:
    return normalize_provider((state or {}).get("provider", ""))


def normalize_instance_name(name: Any) -> str:
    value = str(name or "").strip()
    if not value:
        return ""
    value = value.lstrip("/").strip()
    if "/" in value:
        value = value.split("/")[-1].strip()
    return value


def run_id_from_instance_name(name: Any) -> str:
    instance = normalize_instance_name(name)
    match = re.match(r"^ctfvm-(?:[A-Za-z0-9-]+-)?(\d{8}-\d{6})$", instance)
    if match:
        return match.group(1)
    match = re.search(r"(\d{8}-\d{6})$", instance)
    if match and instance.startswith("ctfvm-"):
        return match.group(1)
    return instance


def normalize_run_id(run_id: Any, instance: Any) -> str:
    rid = str(run_id or "").strip()
    if re.match(r"^\d{8}-\d{6}$", rid):
        return rid
    inferred = run_id_from_instance_name(instance)
    if re.match(r"^\d{8}-\d{6}$", inferred):
        return inferred
    return rid


def iso_started_at_from_run_id(run_id: str) -> str:
    try:
        dt = datetime.strptime(run_id, "%Y%m%d-%H%M%S")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Identity keys, selector, and merge (the dedup core)
# ---------------------------------------------------------------------------


def normalize_run_entry(entry: dict | None) -> dict:
    row = dict(entry or {})
    row["provider"] = provider_from_state(row)
    row["instance"] = normalize_instance_name(row.get("instance", ""))
    row["run_id"] = normalize_run_id(row.get("run_id", ""), row.get("instance", ""))
    row["zone"] = str(row.get("zone", "") or "").strip()
    row["project"] = str(row.get("project", "") or "").strip()
    row["ip"] = str(row.get("ip", "") or "").strip()
    row["started_at"] = str(row.get("started_at", "") or "").strip()
    row["run_key"] = run_selector(row)
    return row


def run_selector(run: dict | None) -> str:
    run = dict(run or {})
    provider = normalize_provider(run.get("provider", ""))
    project = str(run.get("project", "") or "").strip()
    zone = str(run.get("zone", "") or "").strip()
    ip = str(run.get("ip", "") or "").strip()
    instance = normalize_instance_name(run.get("instance", ""))
    run_id = normalize_run_id(run.get("run_id", ""), instance)
    if instance:
        return f"instance:{provider}|{project}|{zone}|{instance}"
    if ip:
        return f"ip:{provider}|{project}|{zone}|{ip}"
    if run_id:
        return f"run:{provider}|{project}|{zone}|{run_id}"
    return ""


def run_identity_keys(run: dict | None) -> list[str]:
    run = normalize_run_entry(run)
    keys: list[str] = []
    provider = str(run.get("provider", "")).strip()
    project = str(run.get("project", "")).strip()
    zone = str(run.get("zone", "")).strip()
    ip = str(run.get("ip", "")).strip()
    instance = str(run.get("instance", "")).strip()
    run_id = str(run.get("run_id", "")).strip()
    if project and zone and ip:
        keys.append(f"ip:{provider}|{project}|{zone}|{ip}")
    elif ip:
        keys.append(f"ip:{provider}|{ip}")
    if project and zone and instance:
        keys.append(f"inst:{provider}|{project}|{zone}|{instance}")
    elif instance:
        keys.append(f"inst:{provider}|{instance}")
    if run_id and not instance:
        keys.append(f"run:{provider}|{run_id}")
    return keys


_SOURCE_RANK = {"local": 0, "current": 1, "discovered": 2}


def merge_run_entries(old: dict | None, new: dict | None) -> dict:
    merged = normalize_run_entry(old)
    new = normalize_run_entry(new)
    old_src = str((old or {}).get("__source", "local"))
    new_src = str((new or {}).get("__source", "local"))
    prefer_new = _SOURCE_RANK.get(new_src, 0) >= _SOURCE_RANK.get(old_src, 0)

    fields = ["run_id", "instance", "zone", "project", "ip", "started_at"]
    if new.get("provider"):
        merged["provider"] = new.get("provider")
    for field in fields:
        old_val = str((old or {}).get(field, "") or "")
        new_val = str((new or {}).get(field, "") or "")
        if prefer_new and new_val:
            merged[field] = new_val
        elif not old_val and new_val:
            merged[field] = new_val
    merged["__source"] = new_src if prefer_new else old_src
    merged["run_key"] = run_selector(merged)
    return merged
