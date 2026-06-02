"""Spawn-job tracker — bounded, thread-safe.

A spawn job runs a provisioning command (today: the bash `ctfvm start`) in a
background thread, captures a tail of its output, and detects the resulting
run_id when it appears. Replaces the ad-hoc ``_spawn_jobs`` dict in the legacy
service.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field

MAX_JOBS = 12
MAX_OUTPUT_BYTES = 64_000
_RUN_ID_RE = re.compile(r"\b(\d{8}-\d{6})\b")


@dataclass
class SpawnJob:
    job_id: str
    state: str = "queued"  # queued | running | done | error
    run_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    output: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class JobLimitError(RuntimeError):
    pass


class SpawnJobTracker:
    def __init__(self, *, max_jobs: int = MAX_JOBS, now=None):
        self._jobs: dict[str, SpawnJob] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._max = max_jobs
        self._seq = 0
        self._now = now or (lambda: "")

    def submit(self, command: list[str], *, runner=None) -> SpawnJob:
        """Start ``command`` in a background thread. ``runner`` is an injection
        point for tests (defaults to a real subprocess)."""
        with self._lock:
            self._evict_if_needed()
            running = sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))
            if running >= self._max:
                raise JobLimitError("spawn job queue full")
            self._seq += 1
            job = SpawnJob(job_id=f"job-{self._seq:04d}", state="queued", started_at=self._now())
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)

        target = runner or self._run_subprocess
        threading.Thread(target=target, args=(job, command), daemon=True).start()
        return job

    def get(self, job_id: str) -> SpawnJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_all(self) -> list[SpawnJob]:
        with self._lock:
            return [self._jobs[jid] for jid in reversed(self._order) if jid in self._jobs]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))

    # --- internals ---------------------------------------------------------
    def _evict_if_needed(self) -> None:
        while len(self._order) >= self._max:
            # drop the oldest finished job; if none finished, stop (limit enforced in submit)
            victim = next((jid for jid in self._order
                           if self._jobs.get(jid) and self._jobs[jid].state in ("done", "error")), None)
            if victim is None:
                return
            self._order.remove(victim)
            self._jobs.pop(victim, None)

    def _append_output(self, job: SpawnJob, text: str) -> None:
        with job._lock:
            job.output = (job.output + text)[-MAX_OUTPUT_BYTES:]
            if job.run_id is None:
                m = _RUN_ID_RE.search(job.output)
                if m:
                    job.run_id = m.group(1)

    def _run_subprocess(self, job: SpawnJob, command: list[str]) -> None:
        job.state = "running"
        try:
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                self._append_output(job, line)
            rc = proc.wait()
            job.finished_at = self._now()
            job.state = "done" if rc == 0 else "error"
        except Exception as exc:  # noqa: BLE001
            self._append_output(job, f"\n[spawn error] {exc}\n")
            job.finished_at = self._now()
            job.state = "error"
