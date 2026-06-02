"""``python -m clanker`` entrypoint.

Phase 1 stub: the CLI is ported command-by-command in later phases (strangler
migration — the bash ``scripts/ctfvm`` remains the primary entrypoint until
then). For now this exposes a couple of read-only introspection commands that
exercise the new core against the real ``.ctfvm`` directory.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .state import RunRegistry


def _cmd_runs(args: argparse.Namespace) -> int:
    registry = RunRegistry()
    listings, current = registry.list_runs()
    rows = [
        {
            "run_id": l.record.run_id,
            "provider": l.record.provider,
            "agent_backend": l.record.agent_backend,
            "instance": l.record.instance,
            "zone": l.record.zone,
            "ip": l.record.ip,
            "run_key": l.run_key,
            "current": l.run_key == current,
        }
        for l in listings
    ]
    if args.json:
        json.dump({"runs": rows, "current_run_id": current}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        if not rows:
            print("(no runs in .ctfvm)")
        for r in rows:
            mark = "*" if r["current"] else " "
            print(f"{mark} {r['run_id'] or '-':<17} {r['provider']:<13} {r['agent_backend']:<11} {r['instance']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clanker", description="CTF automation platform (core)")
    parser.add_argument("--version", action="version", version=f"clanker {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    runs = sub.add_parser("runs", help="list known runs from .ctfvm/ (local only)")
    runs.add_argument("--json", action="store_true", help="emit JSON")
    runs.set_defaults(func=_cmd_runs)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
