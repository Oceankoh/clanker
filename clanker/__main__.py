"""``python -m clanker`` entrypoint.

Strangler migration: the bash ``scripts/ctfvm`` remains the primary entrypoint
and delegates ported command bodies here. Phase 2 ports the read-only commands
``status``, ``cleanup-state``, ``fetch``, ``sync-down`` (plus a local ``runs``
introspection helper). Interactive/streaming commands (attach/shell/vscode/logs)
and provisioning (start/destroy) stay in bash for now.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import __version__, commands
from .agents import SUPPORTED_BACKENDS, build_agent_backend
from .config import Settings
from .controlclient import ControlPlaneError
from .providers import build_provider_registry, build_run_registry
from .state import RunRegistry


def _cmd_runs(args: argparse.Namespace) -> int:
    registry = RunRegistry()  # local-only; no cloud calls
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


def _cmd_status(args: argparse.Namespace) -> int:
    providers = build_provider_registry()
    registry = build_run_registry(providers)
    return commands.cmd_status(registry, providers, run_id=args.run_id, instance=args.instance)


def _cmd_cleanup_state(args: argparse.Namespace) -> int:
    return commands.cmd_cleanup_state(
        build_provider_registry(),
        dry_run=args.dry_run,
        prune_non_running=args.prune_non_running,
    )


def _cmd_fetch(args: argparse.Namespace) -> int:
    return commands.cmd_fetch(
        build_run_registry(),
        run_id=args.run_id,
        instance=args.instance,
        out_dir=args.out,
    )


def _cmd_sync_down(args: argparse.Namespace) -> int:
    return commands.cmd_sync_down(
        build_run_registry(),
        run_id=args.run_id,
        instance=args.instance,
        out_dir=args.out,
        remote_path=args.remote_path,
    )


def _cmd_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "show":
        return commands.cmd_config_show()
    sys.stderr.write("usage: clanker config show\n")
    return 2


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server.app import serve
    serve(args.host, args.port)
    return 0


def _cmd_auth(args: argparse.Namespace) -> int:
    if args.auth_cmd == "claude":
        return commands.cmd_auth_claude(token=args.token)
    if args.auth_cmd == "show":
        return commands.cmd_auth_show()
    sys.stderr.write("usage: clanker auth {claude,show}\n")
    return 2


def _cmd_stage_agent(args: argparse.Namespace) -> int:
    return commands.cmd_stage_agent(
        args.agent, args.staging_dir, model=args.model, ida_mcp_url=args.ida_mcp_url,
    )


def _cmd_render_agent_config(args: argparse.Namespace) -> int:
    backend = build_agent_backend(args.agent)
    settings = Settings()
    ida = args.ida_mcp_url or settings.get("ida_mcp_url", env_var="CTFVM_DEFAULT_IDA_MCP_URL", default="")
    spec = backend.build_spec(model=args.model, ida_mcp_url=ida)
    auth = backend.materialize_auth(settings)

    print(f"# backend: {backend.display_name} ({backend.name})")
    print(f"# launch:  {backend.supervisor_launch_cmd(spec)}")
    print(f"# auth:    env={sorted(auth.container_env)} note={auth.note!r}")
    if auth.local_files:
        print(f"# auth files: {[f.remote_relpath for f in auth.local_files]}")
    for sf in backend.render_config(spec):
        print(f"\n===== {sf.remote_relpath} (mode {sf.mode or 'default'}) =====")
        print(sf.content, end="" if sf.content.endswith("\n") else "\n")
    return 0


def _add_selector(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-id", dest="run_id", default="", help="run id (YYYYmmdd-HHMMSS)")
    p.add_argument("--instance", default="", help="instance name (ctfvm-...)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clanker", description="CTF automation platform (core)")
    parser.add_argument("--version", action="version", version=f"clanker {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    runs = sub.add_parser("runs", help="list known runs from .ctfvm/ (local only)")
    runs.add_argument("--json", action="store_true", help="emit JSON")
    runs.set_defaults(func=_cmd_runs)

    status = sub.add_parser("status", help="show a run's status")
    _add_selector(status)
    status.set_defaults(func=_cmd_status)

    cleanup = sub.add_parser("cleanup-state", help="prune orphaned local state files")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--prune-non-running", action="store_true")
    cleanup.set_defaults(func=_cmd_cleanup_state)

    fetch = sub.add_parser("fetch", help="download findings/artifacts/logs")
    _add_selector(fetch)
    fetch.add_argument("--out", default="", help="output directory")
    fetch.set_defaults(func=_cmd_fetch)

    sync_down = sub.add_parser("sync-down", help="download a remote dir (tar) to local")
    _add_selector(sync_down)
    sync_down.add_argument("--out", default="", help="output directory")
    sync_down.add_argument("--remote-path", dest="remote_path", default="/home/ctf/run/challenge")
    sync_down.set_defaults(func=_cmd_sync_down)

    render = sub.add_parser("render-agent-config", help="render an agent backend's on-VM config")
    render.add_argument("--agent", default="codex", choices=list(SUPPORTED_BACKENDS))
    render.add_argument("--model", default="")
    render.add_argument("--ida-mcp-url", dest="ida_mcp_url", default="")
    render.set_defaults(func=_cmd_render_agent_config)

    auth = sub.add_parser("auth", help="manage agent credentials")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)
    auth_claude = auth_sub.add_parser("claude", help="store a Claude OAuth token (claude setup-token)")
    auth_claude.add_argument("--token", default="", help="token to store (else runs `claude setup-token`)")
    auth_sub.add_parser("show", help="show which agent backends have credentials")
    auth.set_defaults(func=_cmd_auth)

    config = sub.add_parser("config", help="inspect resolved configuration")
    config_sub = config.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("show", help="print effective config with provenance")
    config.set_defaults(func=_cmd_config)

    serve = sub.add_parser("serve", help="run the local web UI / API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=_cmd_serve)

    stage = sub.add_parser("stage-agent", help="materialize an agent payload into a staging dir")
    stage.add_argument("--agent", default="codex", choices=list(SUPPORTED_BACKENDS))
    stage.add_argument("--staging-dir", dest="staging_dir", required=True)
    stage.add_argument("--model", default="")
    stage.add_argument("--ida-mcp-url", dest="ida_mcp_url", default="")
    stage.set_defaults(func=_cmd_stage_agent)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ControlPlaneError as exc:
        sys.stderr.write(f"control plane error: {exc}\n")
        return 1
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(f"local command failed (rc={exc.returncode}): {' '.join(map(str, exc.cmd))}\n")
        return 1
    except OSError as exc:
        # e.g. `tar` binary missing, or a local I/O failure during extract
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
