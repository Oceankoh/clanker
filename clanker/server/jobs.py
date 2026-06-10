"""Spawn-job tracker — bounded, thread-safe.

A spawn job runs a provisioning command (today: the bash `ctfvm start`) in a
background thread, captures a tail of its output, and detects the resulting
run_id when it appears. Replaces the ad-hoc ``_spawn_jobs`` dict in the legacy
service.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time
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
    def __init__(self, *, max_jobs: int = MAX_JOBS, now=None, log_dir=None):
        self._jobs: dict[str, SpawnJob] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._max = max_jobs
        self._seq = 0
        self._now = now or (lambda: "")
        self._log_dir = log_dir or os.path.join(tempfile.gettempdir(), "ctfvm-spawn-logs")

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

    def for_run(self, run_id: str) -> list[SpawnJob]:
        """Jobs whose detected run_id matches — used to surface live
        provisioning output on a run that has no control plane yet."""
        if not run_id:
            return []
        with self._lock:
            return [self._jobs[jid] for jid in reversed(self._order)
                    if jid in self._jobs and self._jobs[jid].run_id == run_id]

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
            os.makedirs(self._log_dir, exist_ok=True)
            log_path = os.path.join(self._log_dir, f"{job.job_id}.log")
            # Detach the provision into its own session and stream its output to a
            # FILE rather than a pipe back to us. Both matter: if the server dies
            # mid-run (closed terminal, crash, restart), `ctfvm start` must keep
            # running to write_state + launch the agent instead of being killed
            # with us — a pipe would break (SIGPIPE) and a shared process group
            # would take the child down. This is the recurring "half-provisioned,
            # control plane up but no agent/tmux" failure.
            wf = open(log_path, "wb")
            try:
                proc = subprocess.Popen(
                    command, stdout=wf, stderr=subprocess.STDOUT, start_new_session=True,
                )
            finally:
                wf.close()  # the child keeps its own dup of the fd
            with open(log_path, "rb") as rf:
                while proc.poll() is None:
                    chunk = rf.read()
                    if chunk:
                        self._append_output(job, chunk.decode("utf-8", "replace"))
                    else:
                        time.sleep(0.1)
                chunk = rf.read()  # drain whatever landed after the last poll
                if chunk:
                    self._append_output(job, chunk.decode("utf-8", "replace"))
            job.finished_at = self._now()
            job.state = "done" if proc.returncode == 0 else "error"
        except Exception as exc:  # noqa: BLE001
            self._append_output(job, f"\n[spawn error] {exc}\n")
            job.finished_at = self._now()
            job.state = "error"
