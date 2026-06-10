"""Regression tests for scripts/ctfvm `refresh_instance_ip` (bash).

Context: `vpn up` over many DigitalOcean workers fanned out a `doctl droplet get
<name>` (a full droplet-list call) per worker, tripping the API rate limit (HTTP
429). The old refresh_instance_ip then clobbered the already-known cached IP (loaded
from the run record) with the empty result, so every tunnel failed with
"Could not determine VM public IP."

These tests source scripts/ctfvm as a library (its `main` is guarded by
BASH_SOURCE == $0), stub `ctfvm_provider_call`, and assert the IP-resolution policy:
prefer the cached IP, only hit the provider when there is none, and never let an
empty/failed lookup overwrite a good cached value.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CTFVM = REPO_ROOT / "scripts" / "ctfvm"


def _run(snippet: str) -> str:
    """Source scripts/ctfvm, run the snippet, return stdout (stripped)."""
    script = (
        f'source "{CTFVM}" >/dev/null 2>&1 || {{ echo "SOURCE_FAIL"; exit 3; }}\n'
        # the sourced script enables `set -euo pipefail`; relax it so an
        # intentionally-failing stub lookup doesn't abort the test harness.
        "set +e +u\n"
        "PROVIDER=digitalocean INSTANCE=ctfvm-tmp-x-20260101-000000 ZONE=sgp1 PROJECT=proj\n"
        f"{snippet}\n"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    self_out = proc.stdout.strip()
    if "SOURCE_FAIL" in self_out:
        raise AssertionError(f"could not source {CTFVM}: {proc.stderr}")
    return self_out


class RefreshInstanceIp(unittest.TestCase):
    def test_cached_ip_survives_failed_lookup(self):
        """A 429/empty live lookup must NOT wipe an IP we already have."""
        out = _run(
            'ctfvm_provider_call() { return 1; }\n'  # simulate doctl 429 -> empty
            'IP="152.42.221.212"; CONTROL_HOST=""\n'
            "refresh_instance_ip\n"
            'echo "IP=${IP}"\n'
            'echo "CONTROL_HOST=${CONTROL_HOST}"\n'
        )
        self.assertIn("IP=152.42.221.212", out)
        # CONTROL_HOST defaults to the (preserved) cached IP
        self.assertIn("CONTROL_HOST=152.42.221.212", out)

    def test_cached_ip_not_reclobbered_even_when_lookup_would_differ(self):
        """With a cached IP, no provider call happens at all (rate-limit safe)."""
        out = _run(
            'ctfvm_provider_call() { echo "9.9.9.9"; }\n'  # would return a different IP
            'IP="152.42.221.212"; CONTROL_HOST=""\n'
            "refresh_instance_ip\n"
            'echo "IP=${IP}"\n'
        )
        self.assertIn("IP=152.42.221.212", out)
        self.assertNotIn("9.9.9.9", out)

    def test_empty_ip_adopts_successful_lookup(self):
        """When there is no cached IP, fall back to the live lookup."""
        out = _run(
            'ctfvm_provider_call() { echo "203.0.113.7"; }\n'
            'IP=""; CONTROL_HOST=""\n'
            "refresh_instance_ip\n"
            'echo "IP=${IP}"\n'
        )
        self.assertIn("IP=203.0.113.7", out)

    def test_empty_ip_and_failed_lookup_stays_empty(self):
        """No cached IP + failed lookup: stay empty, don't crash under set -e."""
        out = _run(
            'ctfvm_provider_call() { return 1; }\n'
            'IP=""; CONTROL_HOST=""\n'
            "refresh_instance_ip\n"
            'echo "IP=[${IP:-}]"\n'
        )
        self.assertIn("IP=[]", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
