"""Phase 1 acceptance: RunRegistry must list/dedup exactly like the old service.

Run from the repo root:
    python tests/test_state_parity.py            # standalone + parity (if importable)
    python -m pytest tests/test_state_parity.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))            # for `import clanker`
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # for the legacy `ctfvm_ui` package

from clanker.identity import run_selector  # noqa: E402
from clanker.models import RunRecord  # noqa: E402
from clanker.state import RunRegistry  # noqa: E402


# --- fixture ----------------------------------------------------------------

def _write_fixture(runs_dir: Path, state_file: Path) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)

    a = {
        "provider": "gcp", "run_id": "20250101-000000",
        "instance": "ctfvm-a-20250101-000000", "zone": "z", "project": "p",
        "ip": "", "started_at": "2025-01-01T00:00:00Z",
    }
    # duplicate of `a` by instance, but carrying the IP — must merge into one row.
    a_dup = dict(a, ip="1.2.3.4")
    b = {
        "provider": "gcp", "run_id": "20250102-000000",
        "instance": "ctfvm-b-20250102-000000", "zone": "z", "project": "p",
        "ip": "5.6.7.8", "started_at": "2025-01-02T00:00:00Z",
        "agent_backend": "claude-code",
    }
    (runs_dir / "20250101-000000.json").write_text(json.dumps(a))
    (runs_dir / "alias-a.json").write_text(json.dumps(a_dup))
    (runs_dir / "20250102-000000.json").write_text(json.dumps(b))
    state_file.write_text(json.dumps(b))  # current run = b


DISCOVERED = RunRecord(
    provider="gcp", run_id="20250103-000000",
    instance="ctfvm-c-20250103-000000", zone="z", project="p",
    ip="9.9.9.9", started_at="2025-01-03T00:00:00Z",
)


# --- standalone behavioral test (always runs) -------------------------------

class RunRegistryBehavior(unittest.TestCase):
    def test_dedup_merge_sort_current(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            state_file = Path(tmp) / "current-run.json"
            _write_fixture(runs_dir, state_file)

            reg = RunRegistry(
                runs_dir=runs_dir,
                state_file=state_file,
                discover=lambda force: [DISCOVERED],
            )
            listings, current = reg.list_runs()

            instances = [l.record.instance for l in listings]
            # 3 unique runs: a (deduped), b, c (discovered)
            self.assertEqual(len(listings), 3)
            # sorted by started_at desc
            self.assertEqual(
                instances,
                ["ctfvm-c-20250103-000000", "ctfvm-b-20250102-000000", "ctfvm-a-20250101-000000"],
            )
            # the two `a` files merged, and the IP from the dup was filled in
            a = next(l for l in listings if l.record.instance.startswith("ctfvm-a"))
            self.assertEqual(a.record.ip, "1.2.3.4")
            # current run points at b
            self.assertEqual(current, run_selector({
                "provider": "gcp", "zone": "z", "project": "p",
                "instance": "ctfvm-b-20250102-000000", "run_id": "20250102-000000",
            }))
            # new field survives the round-trip
            b = next(l for l in listings if l.record.instance.startswith("ctfvm-b"))
            self.assertEqual(b.record.agent_backend, "claude-code")

    def test_agent_backend_defaults_to_codex_for_legacy(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir(parents=True)
            (runs_dir / "r.json").write_text(json.dumps({
                "provider": "gcp", "run_id": "20240101-000000",
                "instance": "ctfvm-x-20240101-000000", "zone": "z", "project": "p",
            }))
            reg = RunRegistry(runs_dir=runs_dir, state_file=runs_dir / "none.json")
            listings, _ = reg.list_runs()
            self.assertEqual(listings[0].record.agent_backend, "codex")


# --- parity vs the legacy service (skipped if it can't be imported) ---------

class ParityWithLegacy(unittest.TestCase):
    def test_matches_old_list_runs(self):
        try:
            import ctfvm_ui.service as legacy  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"legacy ctfvm_ui not importable: {exc}")

        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            state_file = Path(tmp) / "current-run.json"
            _write_fixture(runs_dir, state_file)

            # Point the legacy module's globals at the fixture.
            legacy.RUNS_DIR = runs_dir
            legacy.STATE_FILE = state_file

            class _DummyProviders:
                def discover_runs(self, force=False):
                    return [DISCOVERED.to_state_dict()]

            old = legacy.CTFVMUIService(_DummyProviders())
            old_runs, old_current = old.list_runs()

            reg = RunRegistry(
                runs_dir=runs_dir,
                state_file=state_file,
                discover=lambda force: [DISCOVERED],
            )
            new_listings, new_current = reg.list_runs()

            def norm_old(r):
                return (r.get("run_key"), r.get("run_id"), r.get("instance"),
                        r.get("provider"), r.get("ip"), r.get("started_at"))

            def norm_new(l):
                return (l.run_key, l.record.run_id, l.record.instance,
                        l.record.provider, l.record.ip, l.record.started_at)

            self.assertEqual([norm_old(r) for r in old_runs], [norm_new(l) for l in new_listings])
            self.assertEqual(old_current, new_current)


if __name__ == "__main__":
    unittest.main(verbosity=2)
