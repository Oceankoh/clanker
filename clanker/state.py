"""RunRegistry — the single owner of the known-run set.

Ports ``CTFVMUIService.list_runs`` / ``load_state`` / ``resolve_state`` and the
status cache, with two changes:

  * **One ``RLock`` guards both the discovery cache and the status cache**
    (fixes BUGS.md B4 — the old discovery cache was unguarded).
  * Cloud discovery and per-run status are injected callables so this module has
    no dependency on the provider implementations yet (those arrive as the
    ``CloudProvider`` ABC in Phase 2). With no callables injected it operates on
    local ``.ctfvm`` files only, which is the deterministic core that must stay
    byte-for-byte compatible with the old service.

Invariant 2: no other module reads or writes ``.ctfvm/*.json``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .config import (
    ACTIVE_RUNTIME_STATUSES,
    DISCOVERY_TTL_SECONDS,
    RUNS_DIR,
    STATE_FILE,
    STATUS_CACHE_TTL_SECONDS,
    load_json,
    state_valid,
)
from .identity import (
    merge_run_entries,
    normalize_instance_name,
    normalize_run_entry,
    provider_from_state,
    run_identity_keys,
    run_selector,
)
from .models import RunListing, RunRecord

DiscoverFn = Callable[[bool], list[RunRecord]]   # force -> discovered records
StatusFn = Callable[[RunRecord], str]            # record -> runtime status string


def _runtime_status_is_active(status: str) -> bool:
    return str(status or "").strip().lower() in ACTIVE_RUNTIME_STATUSES


class RunRegistry:
    def __init__(
        self,
        *,
        runs_dir: Path = RUNS_DIR,
        state_file: Path = STATE_FILE,
        discover: DiscoverFn | None = None,
        status: StatusFn | None = None,
    ):
        self._runs_dir = Path(runs_dir)
        self._state_file = Path(state_file)
        self._discover_fn = discover
        self._status_fn = status

        self._lock = threading.RLock()
        self._discovery_cache: dict = {"at": 0.0, "runs": []}  # list[RunRecord]
        self._status_cache: dict[str, dict] = {}

    # --- single-run loading ------------------------------------------------
    def load_state(self, run_id: str | None = None) -> RunRecord | None:
        wanted = str(run_id or "").strip()
        if wanted:
            path = self._runs_dir / f"{wanted}.json"
            if path.exists():
                state = load_json(path)
                return RunRecord.from_mapping(state) if state_valid(state) else None
            current = load_json(self._state_file) if self._state_file.exists() else None
            if state_valid(current) and self._matches_selector(current, wanted):
                return RunRecord.from_mapping(current)
            if self._runs_dir.exists():
                for candidate in sorted(self._runs_dir.glob("*.json")):
                    state = load_json(candidate)
                    if state_valid(state) and self._matches_selector(state, wanted):
                        return RunRecord.from_mapping(state)
            return None

        if not self._state_file.exists():
            return None
        state = load_json(self._state_file)
        return RunRecord.from_mapping(state) if state_valid(state) else None

    def resolve(self, run_id: str | None = None) -> RunRecord | None:
        wanted = str(run_id or "").strip()
        if not wanted:
            return None
        record = self.load_state(wanted)
        if record:
            return record
        listings, _ = self.list_runs()
        for listing in listings:
            r = listing.record
            if wanted in {listing.run_key, r.run_id, r.instance}:
                return r
        return None

    @staticmethod
    def _matches_selector(state: dict, selector: str) -> bool:
        selector = str(selector or "").strip()
        if not selector or not state_valid(state):
            return False
        row = normalize_run_entry(state)
        return selector in {
            str(row.get("run_key", "") or "").strip(),
            str(row.get("run_id", "") or "").strip(),
            str(row.get("instance", "") or "").strip(),
        }

    # --- discovery / status caches (locked) --------------------------------
    def discover(self, force: bool = False) -> list[RunRecord]:
        if self._discover_fn is None:
            return []
        now = time.monotonic()
        with self._lock:
            cache = self._discovery_cache
            if not force and cache["runs"] and (now - cache["at"] < DISCOVERY_TTL_SECONDS):
                return list(cache["runs"])
        discovered = list(self._discover_fn(force))
        with self._lock:
            self._discovery_cache = {"at": now, "runs": list(discovered)}
        return discovered

    def get_status_cached(self, record: RunRecord, force: bool = False) -> str:
        if self._status_fn is None:
            return "UNKNOWN"
        key = self._status_cache_key(record)
        now = time.time()
        with self._lock:
            cached = self._status_cache.get(key)
            if cached and not force and (now - float(cached.get("at", 0))) < STATUS_CACHE_TTL_SECONDS:
                return cached.get("status", "UNKNOWN")
        status = self._status_fn(record)
        with self._lock:
            self._status_cache[key] = {"at": now, "status": status}
        return status

    @staticmethod
    def _status_cache_key(record: RunRecord) -> str:
        return "|".join([record.provider, record.project, record.zone, record.instance, record.run_id])

    # --- the merged listing ------------------------------------------------
    def list_runs(
        self,
        *,
        include_status: bool = False,
        force_status: bool = False,
        force_discovery: bool = False,
        only_live: bool = False,
    ) -> tuple[list[RunListing], str]:
        entries: list[dict] = []

        current = self.load_state()
        current_run_id = run_selector(current.to_state_dict()) if current else ""

        if self._runs_dir.exists():
            for path in sorted(self._runs_dir.glob("*.json")):
                state = load_json(path)
                if not state_valid(state):
                    continue
                run_id = str(state.get("run_id") or path.stem)
                entries.append(
                    normalize_run_entry(
                        {
                            "provider": provider_from_state(state),
                            "run_id": run_id,
                            "instance": state.get("instance", ""),
                            "zone": state.get("zone", ""),
                            "project": state.get("project", ""),
                            "ip": state.get("ip", ""),
                            "started_at": state.get("started_at", ""),
                            "agent_backend": state.get("agent_backend", ""),
                            "__source": "local",
                        }
                    )
                )

        if current and current_run_id:
            cs = current.to_state_dict()
            entries.append(
                normalize_run_entry(
                    {
                        "provider": cs["provider"],
                        "run_id": cs["run_id"],
                        "instance": cs["instance"],
                        "zone": cs["zone"],
                        "project": cs["project"],
                        "ip": cs["ip"],
                        "started_at": cs["started_at"],
                        "agent_backend": cs["agent_backend"],
                        "__source": "current",
                    }
                )
            )

        for record in self.discover(force=force_discovery):
            row = normalize_run_entry(record.to_state_dict())
            row["__source"] = "discovered"
            entries.append(row)

        merged: list[dict] = []
        key_to_idx: dict[str, int] = {}
        for entry in entries:
            idx = None
            for key in run_identity_keys(entry):
                if key in key_to_idx:
                    idx = key_to_idx[key]
                    break
            if idx is None:
                idx = len(merged)
                merged.append(entry)
            else:
                merged[idx] = merge_run_entries(merged[idx], entry)
            for key in run_identity_keys(merged[idx]):
                key_to_idx[key] = idx

        merged.sort(key=lambda run: run.get("started_at", ""), reverse=True)

        # enrich with runtime status
        if include_status or only_live:
            for entry in merged:
                record = self._entry_to_record(entry)
                status = self.get_status_cached(record, force=force_status) if record else "UNKNOWN"
                entry["runtime_status"] = status
                entry["is_runtime_active"] = _runtime_status_is_active(status)

        if only_live:
            live: list[dict] = []
            for entry in merged:
                source = str(entry.get("__source", "") or "")
                status = str(entry.get("runtime_status", "") or "").strip()
                if source == "discovered" or entry.get("is_runtime_active"):
                    live.append(entry)
                    continue
                if status == "":
                    self._prune_local_state(entry)
            merged = live
            if current_run_id and not any(str(e.get("run_key", "")) == current_run_id for e in merged):
                current_run_id = ""

        if not current_run_id:
            for entry in merged:
                candidate = str(entry.get("run_key", "") or "").strip()
                if candidate:
                    current_run_id = candidate
                    break

        listings = [
            RunListing(
                record=self._entry_to_record(entry),
                run_key=str(entry.get("run_key", "") or ""),
                runtime_status=str(entry.get("runtime_status", "") or ""),
                is_runtime_active=bool(entry.get("is_runtime_active", False)),
                source=str(entry.get("__source", "") or ""),
            )
            for entry in merged
        ]
        return listings, current_run_id

    @staticmethod
    def _entry_to_record(entry: dict) -> RunRecord:
        return RunRecord.from_mapping(entry)

    def _prune_local_state(self, entry: dict) -> None:
        """Port of legacy ``_prune_local_state_for_run``: remove the per-run state
        files AND clear ``current-run.json`` if it points at the pruned run, so a
        destroyed run that happened to be the current one stops being selected."""
        entry = normalize_run_entry(entry)
        run_id = str(entry.get("run_id", "") or "").strip()
        instance = str(entry.get("instance", "") or "").strip()
        for name in (run_id, instance):
            if not name:
                continue
            try:
                (self._runs_dir / f"{name}.json").unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

        current = load_json(self._state_file) if self._state_file.exists() else None
        if not current:
            return
        current_run_id = str((current or {}).get("run_id", "") or "").strip()
        current_instance = normalize_instance_name((current or {}).get("instance", ""))
        if (run_id and current_run_id == run_id) or (instance and current_instance == instance):
            try:
                self._state_file.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
