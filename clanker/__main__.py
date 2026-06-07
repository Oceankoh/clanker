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


def _cmd_upload(args: argparse.Namespace) -> int:
    return commands.cmd_upload(
        build_run_registry(),
        run_id=args.run_id,
        instance=args.instance,
        local_path=args.local_path,
        remote_path=args.remote_path,
        as_tar=args.tar,
        allow_abs=args.allow_abs,
        mode=args.mode,
    )


def _cmd_worker(args: argparse.Namespace) -> int:
    import io
    import tarfile
    from .server.service import ApiError, UiService

    svc = UiService()
    try:
        if args.worker_cmd == "spawn":
            payload = {"count": args.count, "provider": args.provider, "zone": args.zone,
                       "project": args.project, "size_slug": args.size_slug,
                       "machine_type": args.machine_type}
            cmds = svc.worker_start_commands({k: v for k, v in payload.items() if v})
            rc = 0
            for cmd in cmds:
                print("+", " ".join(cmd))
                rc |= subprocess.call(cmd)
            return 1 if rc else 0
        if args.worker_cmd == "ls":
            groups = svc.list_workers()
            if not groups:
                print("(no workers)")
            for g in groups:
                w = g["worker"].record
                print(f"{w.challenge_name or w.run_id:<14} {w.provider:<13} {len(g['challenges'])} challenge(s)  {w.ip}")
            return 0
        if args.worker_cmd == "show":
            for g in svc.list_workers():
                w = g["worker"].record
                if args.worker not in (w.run_id, w.instance, w.challenge_name):
                    continue
                print(f"{w.challenge_name or w.run_id}  ({w.provider} {w.ip})")
                for c in g["challenges"]:
                    cr = c.record
                    print(f"  - {cr.challenge_name:<16} {cr.agent_backend:<11} {cr.tmux_session}  run_id={cr.run_id}")
                return 0
            sys.stderr.write(f"worker not found: {args.worker}\n")
            return 1
        if args.worker_cmd == "add":
            from pathlib import Path as _P
            src = _P(args.dir).expanduser()
            if not src.is_dir():
                sys.stderr.write(f"challenge dir not found: {src}\n")
                return 1
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                tf.add(str(src), arcname=".")
            # mirror a normal run: default desc/ideas from the folder's files
            def _read(name):
                try:
                    return (src / name).read_text().strip()
                except OSError:
                    return ""
            payload = {"name": args.name or src.name, "agent_backend": args.agent,
                       "description": args.desc or _read("description.txt"),
                       "ideas": args.ideas or _read("ideas.txt"),
                       "flag_format": args.flag_format,
                       "model": args.model, "reasoning_effort": args.reasoning_effort,
                       "account": args.account}
            res = svc.add_challenge(args.worker, {k: v for k, v in payload.items() if v}, buf.getvalue())
            print(f"Added challenge {res['slug']} to {args.worker}: "
                  f"run_id={res['run_id']} session={res['tmux_session']}")
            return 0
        if args.worker_cmd == "rm-challenge":
            res = svc.remove_challenge(args.worker, args.slug)
            print(f"Removed {res['slug']} from {res['worker_id']} (run_id={res['removed_run_id']})")
            return 0
    except ApiError as exc:
        sys.stderr.write(f"{exc.message}\n")
        return 1
    sys.stderr.write("usage: clanker worker {spawn,ls,show,add,rm-challenge}\n")
    return 2


def _cmd_image(args: argparse.Namespace) -> int:
    if args.image_cmd == "status":
        return commands.cmd_image_status()
    if args.image_cmd == "id":
        return commands.cmd_image_id(args.provider)
    if args.image_cmd == "bake":
        return commands.cmd_image_bake(args.provider)
    if args.image_cmd == "record":
        return commands.cmd_image_record(
            args.provider, image_id=args.image_id, image_name=args.image_name,
            built_at=args.built_at, codex_version=args.codex_version,
            claude_version=args.claude_version,
        )
    sys.stderr.write("usage: clanker image {status,bake,record}\n")
    return 2


def _cmd_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "show":
        return commands.cmd_config_show()
    if args.config_cmd == "profiles":
        return commands.cmd_config_profiles()
    sys.stderr.write("usage: clanker config {show,profiles}\n")
    return 2


def _cmd_agents(args: argparse.Namespace) -> int:
    return commands.cmd_agents()


def _cmd_fanout(args: argparse.Namespace) -> int:
    return commands.cmd_fanout(args.root, provider=args.provider, agent=args.agent,
                               model=args.model, reasoning_effort=args.reasoning_effort)


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server.app import serve
    token = args.token or str(Settings().get("ui_token") or "")
    serve(args.host, args.port, auth_token=token)
    return 0


def _cmd_share(args: argparse.Namespace) -> int:
    from .share import run_share
    return run_share(args.host, args.port)


def _cmd_auth(args: argparse.Namespace) -> int:
    if args.auth_cmd == "claude":
        return commands.cmd_auth_claude(token=args.token, name=args.name)
    if args.auth_cmd == "codex":
        return commands.cmd_auth_codex(args.name, api_key=args.api_key, codex_home=args.codex_home)
    if args.auth_cmd == "show":
        return commands.cmd_auth_show()
    sys.stderr.write("usage: clanker auth {claude,codex,show}\n")
    return 2


def _cmd_stage_agent(args: argparse.Namespace) -> int:
    return commands.cmd_stage_agent(
        args.agent, args.staging_dir, model=args.model,
        reasoning_effort=args.reasoning_effort, ida_mcp_url=args.ida_mcp_url,
        account=args.account,
    )


def _cmd_render_agent_config(args: argparse.Namespace) -> int:
    backend = build_agent_backend(args.agent)
    settings = Settings()
    ida = args.ida_mcp_url or settings.get("ida_mcp_url", env_var="CTFVM_DEFAULT_IDA_MCP_URL", default="")
    spec = backend.build_spec(model=args.model, reasoning_effort=args.reasoning_effort, ida_mcp_url=ida)
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

    agents = sub.add_parser("agents", help="list agent backends + readiness")
    agents.set_defaults(func=_cmd_agents)

    fanout = sub.add_parser("fanout", help="deploy a folder of challenges — one VM per subfolder")
    fanout.add_argument("root", help="folder whose immediate subfolders are challenges")
    fanout.add_argument("--provider", default="")
    fanout.add_argument("--agent", default="")
    fanout.add_argument("--model", default="")
    fanout.add_argument("--reasoning-effort", dest="reasoning_effort", default="",
                        help="codex model_reasoning_effort (blank = xhigh)")
    fanout.set_defaults(func=_cmd_fanout)

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

    upload = sub.add_parser("upload", help="push a file (or --tar a directory) to a running run")
    _add_selector(upload)
    upload.add_argument("local_path", help="local file (or directory with --tar) to upload")
    upload.add_argument("remote_path", nargs="?", default="",
                        help="remote dest (default: workspace/<basename>); confined to the workspace")
    upload.add_argument("--tar", action="store_true", help="local_path is a directory; tar + extract into remote_path")
    upload.add_argument("--allow-abs", dest="allow_abs", action="store_true",
                        help="permit an absolute remote path outside the run workspace")
    upload.add_argument("--mode", default="", help="octal file mode for the uploaded file (e.g. 0755)")
    upload.set_defaults(func=_cmd_upload)

    render = sub.add_parser("render-agent-config", help="render an agent backend's on-VM config")
    render.add_argument("--agent", default="codex", choices=list(SUPPORTED_BACKENDS))
    render.add_argument("--model", default="")
    render.add_argument("--reasoning-effort", dest="reasoning_effort", default="",
                        help="codex model_reasoning_effort (blank = xhigh)")
    render.add_argument("--ida-mcp-url", dest="ida_mcp_url", default="")
    render.set_defaults(func=_cmd_render_agent_config)

    auth = sub.add_parser("auth", help="manage agent credentials")
    auth_sub = auth.add_subparsers(dest="auth_cmd", required=True)
    auth_claude = auth_sub.add_parser("claude", help="store a Claude OAuth token (claude setup-token)")
    auth_claude.add_argument("--token", default="", help="token to store (else runs `claude setup-token`)")
    auth_claude.add_argument("--name", default="", help="store under a named credential profile")
    auth_codex = auth_sub.add_parser("codex", help="store a named Codex credential profile")
    auth_codex.add_argument("--name", required=True, help="profile name")
    auth_codex.add_argument("--api-key", dest="api_key", default="", help="OPENAI_API_KEY for this account")
    auth_codex.add_argument("--codex-home", dest="codex_home", default="",
                            help="local dir holding this account's ~/.codex/auth.json")
    auth_sub.add_parser("show", help="show which agent backends have credentials")
    auth.set_defaults(func=_cmd_auth)

    worker = sub.add_parser("worker", help="empty worker VMs that host many challenges")
    worker_sub = worker.add_subparsers(dest="worker_cmd", required=True)
    w_spawn = worker_sub.add_parser("spawn", help="provision N empty worker VMs")
    w_spawn.add_argument("--count", type=int, required=True, help="how many workers to spawn")
    w_spawn.add_argument("--provider", default="")
    w_spawn.add_argument("--zone", default="")
    w_spawn.add_argument("--project", default="")
    w_spawn.add_argument("--size-slug", dest="size_slug", default="")
    w_spawn.add_argument("--machine-type", dest="machine_type", default="")
    worker_sub.add_parser("ls", help="list workers + hosted challenge counts")
    w_show = worker_sub.add_parser("show", help="show a worker's hosted challenges")
    w_show.add_argument("worker", help="worker run_id / instance / name")
    w_add = worker_sub.add_parser("add", help="add a challenge to a worker (upload + launch)")
    w_add.add_argument("worker", help="worker run_id / instance / name")
    w_add.add_argument("--dir", required=True, help="local challenge folder")
    w_add.add_argument("--name", default="", help="challenge name (default: folder name)")
    w_add.add_argument("--agent", default="", help="agent backend (default: worker's)")
    w_add.add_argument("--desc", default="", help="challenge description (default: description.txt in the folder)")
    w_add.add_argument("--ideas", default="", help="starting ideas (default: ideas.txt in the folder)")
    w_add.add_argument("--flag-format", dest="flag_format", default="", help="expected flag format, folded into the prompt")
    w_add.add_argument("--model", default="")
    w_add.add_argument("--reasoning-effort", dest="reasoning_effort", default="")
    w_add.add_argument("--account", default="", help="credential profile")
    w_rm = worker_sub.add_parser("rm-challenge", help="stop a challenge on a worker")
    w_rm.add_argument("worker", help="worker run_id / instance / name")
    w_rm.add_argument("slug", help="challenge slug (see `worker show`)")
    worker.set_defaults(func=_cmd_worker)

    image = sub.add_parser("image", help="golden VM image: status / bake / record")
    image_sub = image.add_subparsers(dest="image_cmd", required=True)
    image_sub.add_parser("status", help="show when each provider's golden image was built")
    image_id = image_sub.add_parser("id", help="print the effective golden image id (for the provisioner)")
    image_id.add_argument("--provider", required=True)
    image_bake = image_sub.add_parser("bake", help="rebuild the golden image (attended; provisions a builder VM)")
    image_bake.add_argument("--provider", required=True, help="digitalocean | gcp")
    image_record = image_sub.add_parser("record", help="persist golden-image metadata (used by bake.sh)")
    image_record.add_argument("--provider", required=True)
    image_record.add_argument("--image-id", dest="image_id", required=True)
    image_record.add_argument("--image-name", dest="image_name", default="")
    image_record.add_argument("--built-at", dest="built_at", default="")
    image_record.add_argument("--codex-version", dest="codex_version", default="")
    image_record.add_argument("--claude-version", dest="claude_version", default="")
    image.set_defaults(func=_cmd_image)

    config = sub.add_parser("config", help="inspect resolved configuration")
    config_sub = config.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("show", help="print effective config with provenance")
    config_sub.add_parser("profiles", help="list credential profiles")
    config.set_defaults(func=_cmd_config)

    serve = sub.add_parser("serve", help="run the local web UI / API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token", default="", help="require this UI token (else CTFVM_UI_TOKEN, else open)")
    serve.set_defaults(func=_cmd_serve)

    share = sub.add_parser("share", help="expose the UI via ngrok with a tokenized link")
    share.add_argument("--host", default="127.0.0.1")
    share.add_argument("--port", type=int, default=8765)
    share.set_defaults(func=_cmd_share)

    stage = sub.add_parser("stage-agent", help="materialize an agent payload into a staging dir")
    stage.add_argument("--agent", default="codex", choices=list(SUPPORTED_BACKENDS))
    stage.add_argument("--staging-dir", dest="staging_dir", required=True)
    stage.add_argument("--model", default="")
    stage.add_argument("--reasoning-effort", dest="reasoning_effort", default="",
                       help="codex model_reasoning_effort (blank = xhigh)")
    stage.add_argument("--ida-mcp-url", dest="ida_mcp_url", default="")
    stage.add_argument("--account", default="", help="credential profile to use (see `clanker auth`)")
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
