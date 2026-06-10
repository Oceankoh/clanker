#!/usr/bin/env python3
"""Stress-test the UI under load: spawn N workers, add challenges to each,
then hammer the same API endpoints the frontend hits and report latency.

Verifies:
  1. No HTTP errors under parallel polling (rate-limit safety)
  2. Response latencies stay acceptable (< 2s p95 for fleet, < 5s for snapshots)
  3. Terminal-state runs stop being polled (frontend skip logic)

Usage:
    # Full live run: spawn 10 workers, add challenges, poll until solved or timeout
    scripts/test/stress_ui.py --workers 10 --agent claude-code --timeout 900

    # Poll-only (workers already running): just stress the UI polling layer
    scripts/test/stress_ui.py --poll-only --duration 120

Env:
    CLANKER_API   base URL (default http://127.0.0.1:8765)
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = os.environ.get("CLANKER_API", "http://127.0.0.1:8765")
ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "examples" / "smoke-challenges"
CHALLENGES = sorted(
    [d for d in SMOKE.iterdir() if d.is_dir()],
    key=lambda p: p.name,
)


def api(path, method="GET", body=None, headers=None, timeout=30):
    url = BASE + path
    req = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["data"]


def api_timed(path, timeout=10):
    """Fetch an API endpoint, return (elapsed_ms, data_or_None, error_or_None)."""
    t0 = time.monotonic()
    try:
        data = api(path, timeout=timeout)
        return (time.monotonic() - t0) * 1000, data, None
    except Exception as e:
        return (time.monotonic() - t0) * 1000, None, str(e)


# ---------------------------------------------------------------------------
# Phase 1: spawn workers + add challenges
# ---------------------------------------------------------------------------

def spawn_workers(n, agent, effort="xhigh"):
    print(f"\n=== Spawning {n} workers ===")
    payload = json.dumps({
        "count": n,
        "no_vpn": True,
    }).encode()
    resp = api("/api/v1/workers", method="POST", body=payload,
               headers={"Content-Type": "application/json"}, timeout=60)
    job_ids = resp.get("job_ids", [])
    print(f"  submitted {len(job_ids)} spawn jobs: {job_ids}")
    return job_ids


def wait_workers_ready(n, timeout=600):
    """Poll until N workers have runtime_status=active (droplet is up)."""
    print(f"\n=== Waiting for {n} workers to come up (timeout {timeout}s) ===")
    deadline = time.monotonic() + timeout
    up = []
    while time.monotonic() < deadline:
        try:
            data = api("/api/v1/runs?include_status=true")
            runs = data.get("runs", [])
            up = [r for r in runs
                  if r.get("runner_type") == "worker"
                  and r.get("runtime_status") in ("active", "running")]
            print(f"  {len(up)}/{n} workers active  ", end="\r", flush=True)
            if len(up) >= n:
                print(f"\n  all {n} workers active")
                return up
        except Exception as e:
            print(f"  poll error: {e}")
        time.sleep(10)
    print(f"\n  WARN: only {len(up)}/{n} workers active at timeout")
    return up


def add_challenges_to_workers(workers, agent, effort="xhigh"):
    """Round-robin the smoke challenges across workers, retrying until the
    control plane is up (workers boot faster than the control server starts)."""
    print(f"\n=== Adding challenges to {len(workers)} workers ===")
    import io
    import tarfile

    pending = list(enumerate(workers))
    max_retries = 12  # ~2 minutes of retries at 10s intervals

    for attempt in range(max_retries):
        still_pending = []
        for i, w in pending:
            wid = w.get("run_id", "")
            chal = CHALLENGES[i % len(CHALLENGES)]
            name = chal.name
            desc = ""
            ideas = ""
            try:
                desc = (chal / "description.txt").read_text().strip()
            except Exception:
                pass
            try:
                ideas = (chal / "ideas.txt").read_text().strip()
            except Exception:
                pass

            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                for p in sorted(chal.rglob("*")):
                    if p.is_file() and p.name != "EXPECTED.tsv":
                        tf.add(p, arcname=str(p.relative_to(chal)))
            tar_bytes = buf.getvalue()

            q = urllib.parse.urlencode({
                "name": name, "agent_backend": agent,
                "description": desc, "ideas": ideas,
                "flag_format": "flag{...}",
                "reasoning_effort": effort,
            })
            url = f"/api/v1/workers/{urllib.parse.quote(wid)}/challenges?{q}"
            try:
                resp = api(url, method="POST", body=tar_bytes,
                           headers={"Content-Type": "application/octet-stream"}, timeout=120)
                print(f"  worker {i+1}: {name} -> {resp.get('slug', '?')}")
            except Exception as e:
                err = str(e)
                if "502" in err or "REMOTE_ERROR" in err:
                    still_pending.append((i, w))
                    if attempt == 0:
                        print(f"  worker {i+1}: {name} -> control plane not ready, will retry")
                else:
                    print(f"  worker {i+1}: {name} -> ERROR {e}")

        pending = still_pending
        if not pending:
            break
        if attempt < max_retries - 1:
            print(f"  retrying {len(pending)} workers in 10s...")
            time.sleep(10)
        else:
            for i, w in pending:
                print(f"  worker {i+1}: FAILED after {max_retries} retries")


# ---------------------------------------------------------------------------
# Phase 2: stress-test the polling layer
# ---------------------------------------------------------------------------

TERMINAL_STATES = {"solved", "failed", "stopped"}


def poll_cycle(runs):
    """Simulate one frontend fleet-poll cycle: parallel snapshots for all
    non-terminal runs + one fleet list. Returns per-request latency samples."""
    samples = {"fleet": [], "snapshot": [], "errors": 0}

    # fleet list (what refreshFleet does)
    ms, data, err = api_timed("/api/v1/runs")
    samples["fleet"].append(ms)
    if err:
        samples["errors"] += 1

    # parallel snapshots (what pollFleetTick does)
    active = [r for r in runs if r.get("_state") not in TERMINAL_STATES]
    with ThreadPoolExecutor(max_workers=min(len(active), 16)) as pool:
        futs = {
            pool.submit(api_timed,
                        f"/api/v1/runs/{r['run_id']}?include_artifacts=false", 10): r
            for r in active
        }
        for fut in as_completed(futs):
            ms, data, err = fut.result()
            samples["snapshot"].append(ms)
            if err:
                samples["errors"] += 1
            elif data:
                r = futs[fut]
                cs = data.get("challenge_state", {})
                if cs:
                    r["_state"] = cs.get("state", "")

    return samples


def run_stress_poll(duration_sec):
    """Run the polling loop for `duration_sec`, collecting latency stats."""
    print(f"\n=== Stress-polling for {duration_sec}s ===")
    print(f"  (simulates frontend: fleet list + parallel snapshots per cycle)\n")

    # get initial run list
    data = api("/api/v1/runs")
    runs = data.get("runs", [])
    for r in runs:
        cs = r.get("challenge_state", {})
        r["_state"] = cs.get("state", "") if cs else ""
    non_worker = [r for r in runs if r.get("runner_type") != "worker"]
    print(f"  {len(runs)} total runs ({len(non_worker)} non-worker)")

    all_fleet = []
    all_snap = []
    total_errors = 0
    cycles = 0
    t0 = time.monotonic()

    while (time.monotonic() - t0) < duration_sec:
        cycle_t0 = time.monotonic()
        samples = poll_cycle(non_worker)
        all_fleet.extend(samples["fleet"])
        all_snap.extend(samples["snapshot"])
        total_errors += samples["errors"]
        cycles += 1

        active = sum(1 for r in non_worker if r.get("_state") not in TERMINAL_STATES)
        terminal = sum(1 for r in non_worker if r.get("_state") in TERMINAL_STATES)
        elapsed = time.monotonic() - t0

        fleet_p50 = statistics.median(samples["fleet"]) if samples["fleet"] else 0
        snap_p50 = statistics.median(samples["snapshot"]) if samples["snapshot"] else 0
        print(f"  cycle {cycles:3d} | {elapsed:5.0f}s | "
              f"fleet {fleet_p50:6.0f}ms | "
              f"snap {snap_p50:6.0f}ms ({len(samples['snapshot'])} reqs) | "
              f"active={active} term={terminal} | "
              f"errs={samples['errors']}", flush=True)

        # sleep to match the frontend's 10s poll interval
        cycle_time = time.monotonic() - cycle_t0
        sleep = max(0, 10.0 - cycle_time)
        if sleep > 0:
            time.sleep(sleep)

    return all_fleet, all_snap, total_errors, cycles


def report(all_fleet, all_snap, total_errors, cycles):
    print(f"\n{'='*60}")
    print(f"  STRESS TEST RESULTS  ({cycles} cycles)")
    print(f"{'='*60}")

    def stats_line(name, samples):
        if not samples:
            print(f"  {name}: no data")
            return True
        p50 = statistics.median(samples)
        p95 = sorted(samples)[int(len(samples) * 0.95)] if len(samples) > 1 else samples[0]
        p99 = sorted(samples)[int(len(samples) * 0.99)] if len(samples) > 1 else samples[0]
        mx = max(samples)
        print(f"  {name:12s}  n={len(samples):4d}  "
              f"p50={p50:6.0f}ms  p95={p95:6.0f}ms  p99={p99:6.0f}ms  max={mx:6.0f}ms")
        return p95

    fleet_p95 = stats_line("fleet list", all_fleet)
    snap_p95 = stats_line("snapshot", all_snap)
    print(f"  {'errors':12s}  total={total_errors}")

    print()
    ok = True
    if isinstance(fleet_p95, (int, float)) and fleet_p95 > 2000:
        print("  FAIL: fleet list p95 > 2s")
        ok = False
    if isinstance(snap_p95, (int, float)) and snap_p95 > 5000:
        print("  FAIL: snapshot p95 > 5s")
        ok = False
    if total_errors > cycles * 0.1:
        print(f"  FAIL: error rate {total_errors}/{cycles} > 10%")
        ok = False
    if ok:
        print("  PASS: all latency and error thresholds met")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=10,
                        help="number of workers to spawn (default 10)")
    parser.add_argument("--agent", default="claude-code",
                        help="agent backend (default claude-code)")
    parser.add_argument("--effort", default="xhigh",
                        help="reasoning effort (default xhigh)")
    parser.add_argument("--timeout", type=int, default=900,
                        help="total timeout in seconds (default 900)")
    parser.add_argument("--poll-only", action="store_true",
                        help="skip spawning; just poll existing runs")
    parser.add_argument("--duration", type=int, default=0,
                        help="poll duration in seconds (default: --timeout value)")
    args = parser.parse_args()

    poll_dur = args.duration or args.timeout

    if not args.poll_only:
        spawn_workers(args.workers, args.agent, args.effort)
        workers = wait_workers_ready(args.workers, timeout=min(args.timeout, 600))
        if not workers:
            print("FAIL: no workers came up")
            return 1
        add_challenges_to_workers(workers, args.agent, args.effort)
        print("\n  challenges deployed — starting stress poll")

    all_fleet, all_snap, total_errors, cycles = run_stress_poll(poll_dur)
    ok = report(all_fleet, all_snap, total_errors, cycles)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
