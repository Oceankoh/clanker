#!/usr/bin/env python3
"""Poll every run via the UI API and report, per non-worker run: challenge_state,
whether the agent's transcript shows activity (proxy for "system prompt landed"),
and whether the EXPECTED.tsv flag appears in findings/panes/transcript.

Prints a matrix table + an N/total flag tally. Exit 0 always (it's a reporter);
parse the tally line for pass/fail in CI.

Usage:   scripts/test/assert_flags.py
Env:     CLANKER_API   base URL (default http://127.0.0.1:8765)
"""
import base64
import json
import os
import urllib.request
from pathlib import Path

BASE = os.environ.get("CLANKER_API", "http://127.0.0.1:8765")
ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {}
for tsv in [ROOT / "examples/smoke-challenges/EXPECTED.tsv",
            ROOT / "examples/lab-challenges/EXPECTED.tsv"]:
    if tsv.is_file():
        for line in tsv.read_text().splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                EXPECTED[k.strip()] = v.strip()


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)["data"]


def b64(s):
    try:
        return base64.b64decode(s or "").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def main():
    runs = api("/api/v1/runs")["runs"]
    rows = []
    for r in runs:
        if r.get("runner_type") == "worker":
            continue  # the host itself solves nothing
        rid, slug = r["run_id"], (r.get("name") or "").strip()
        exp = EXPECTED.get(slug, "")
        if not exp:  # dedup may prefix the name (e.g. smoke-challenges-01-strings)
            for k, v in EXPECTED.items():
                if slug.endswith(k):
                    slug, exp = k, v
                    break
        try:
            snap = api(f"/api/v1/runs/{rid}?include_artifacts=false")
        except Exception as e:  # noqa: BLE001
            rows.append((slug, r.get("agent_backend"), bool(r.get("parent_worker_id")), rid, "ERR", "-", "-", str(e)[:30]))
            continue
        cs = (snap.get("challenge_state") or {}).get("state", "?")
        hay = b64(snap.get("findings_b64")) + " " + \
            " ".join(b64(p.get("output_b64", "")) for p in (snap.get("panes") or []))
        try:
            tr = json.dumps(api(f"/api/v1/runs/{rid}/transcript"))
        except Exception:  # noqa: BLE001
            tr = ""
        prompt = "Y" if (tr and tr != "{}" and len(tr) > 50) else "-"
        flag = "Y" if (exp and exp in (hay + tr)) else "-"
        rows.append((slug, r.get("agent_backend"), bool(r.get("parent_worker_id")), rid, cs, prompt, flag, exp))

    print(f"{'slug':<14}{'agent':<12}{'rt':<4}{'run_id':<18}{'state':<13}{'prmpt':<6}{'flag':<5}expected")
    solved = 0
    for slug, ag, wkr, rid, cs, pl, ff, exp in sorted(rows, key=lambda x: (x[2], x[1], x[0])):
        solved += ff == "Y"
        print(f"{slug:<14}{(ag or '?'):<12}{('wkr' if wkr else 'nrm'):<4}{rid:<18}{cs:<13}{pl:<6}{ff:<5}{exp}")
    print(f"\nflags found: {solved}/{len(rows)}")


if __name__ == "__main__":
    main()
