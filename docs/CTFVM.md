# Disposable CTF VM with Codex CLI

## Prerequisites
- One cloud provider configured:
  - GCP: `gcloud` installed and authenticated (`gcloud auth login`)
  - DigitalOcean: `doctl` installed/authenticated
- Default zone/project (GCP) or region (DigitalOcean) configured, or passed to `ctfvm start`
- Local Codex login complete (`codex login`)

Optional break-glass access:
- SSH access is still useful for `ctfvm attach`, `ctfvm shell`, and `ctfvm vscode`.
- On DigitalOcean that means uploading at least one SSH key, but it is no longer required for the normal HTTP control-plane flow.

## Repo `.env` Defaults
`./scripts/ctfvm` now auto-loads a repo-local `.env` file before it resolves defaults.

Precedence is:
- CLI flags
- repo `.env`
- cloud CLI environment/defaults such as `gcloud config`
- saved toolbox metadata in `.ctfvm/config.json`
- built-in script defaults

The repo now includes a starter [.env](../.env) with the main knobs:
- provider: `CTFVM_PROVIDER`
- GCP project/zone/machine defaults: `CTFVM_GCP_*`
- DigitalOcean region/size/SSH key defaults: `CTFVM_DO_*`
- base-network attachment defaults: `CTFVM_GCP_NETWORK`, `CTFVM_GCP_SUBNET`, `CTFVM_GCP_NETWORK_TAGS`, `CTFVM_DO_VPC_ID`, `CTFVM_DO_TAGS`
- control-plane port: `CTFVM_CONTROL_PORT`
- local archive path: `CTFVM_LOCAL_IMAGE_ARCHIVE`

The default control-plane port is `443`. That avoids local networks that silently block outbound high ports such as `8777`, while keeping the transport HTTP-based.

Example:

```bash
CTFVM_PROVIDER=digitalocean
CTFVM_DO_REGION=nyc3
CTFVM_DO_SSH_KEYS=aa:bb:cc:dd:...,11:22:33:44:...
```

## Commands
From repo root:

```bash
./scripts/ctfvm image push-registry --provider gcp
./scripts/ctfvm image push-registry --provider digitalocean --location nyc3 --repository my-ctf-registry
./scripts/ctfvm start --dir ./challenge --desc "..." --ideas "..."
./scripts/ctfvm start --provider digitalocean --zone nyc3 --use-local-image --dir ./challenge --desc "..." --ideas "..."
./scripts/ctfvm attach
./scripts/ctfvm chat
./scripts/ctfvm shell
./scripts/ctfvm vscode
./scripts/ctfvm monitor
./scripts/ctfvm vpn status
./scripts/ctfvm vpn up --local-cidr 192.168.56.0/24
./scripts/ctfvm vpn down
./scripts/ctfvm mcp add ida --url http://10.1.1.8:8080/mcp
./scripts/ctfvm ui
./scripts/ctfvm inject --msg "Try the heap unlink path"
./scripts/ctfvm sync-skill --skill ctf-exploit-subagent
./scripts/ctfvm sync-skill --skill ctf-docs-subagent
./scripts/ctfvm sync-skill --skill webhook-site-callbacks
./scripts/ctfvm sync-down --out ./live-copy
./scripts/ctfvm sync-up --src ./live-copy
./scripts/ctfvm logs
./scripts/ctfvm fetch --out ./outputs
./scripts/ctfvm destroy
```

## Architecture Overview
The system now has two planes:

1. Control plane:
   - a small HTTP service runs on every VM
   - the local CLI and local web UI talk to that service for remote exec, uploads, and downloads
2. Work plane:
   - the VM runs `ctf-toolbox`
   - the Codex supervisor and subagents live inside tmux sessions in that environment

Main components:
- local orchestrator: `scripts/ctfvm`
- local dashboard: `scripts/ctfvm_ui/*`
- VM bootstrap: `vm/startup.sh`
- VM control service: `vm/control_server.py`
- provider adapters: `scripts/lib/ctfvm/providers/*`
- static infra layer: `infra/terraform/gcp` and `infra/terraform/digitalocean`

High-level flow:
1. `ctfvm start` creates a VM on GCP or DigitalOcean.
2. The startup script installs Docker, creates the `ctf` user, and starts the HTTP control-plane service.
3. The local machine waits for that HTTP endpoint instead of waiting for a fresh SSH session.
4. Challenge files, prompts, skills, and config are uploaded through the control plane.
5. The toolbox container starts and launches the tmux-based supervisor.
6. The local UI polls the same control plane to render findings, pane output, and artifacts.

Security model:
- per-run HTTP Basic Auth credentials
- provider firewall or VPC controls around the control-plane port
- SSH kept only as optional break-glass access

Terraform split:
- Terraform should manage long-lived shared resources like VPCs, subnets, and firewall rules.
- `ctfvm` should keep managing short-lived challenge VMs dynamically.

Load balancer:
- not required for the current local-first setup
- only worth adding if you later host the aggregator in the cloud and want a single stable entrypoint

The higher-level design note lives in [HTTP_CONTROL_PLANE_ARCHITECTURE.md](HTTP_CONTROL_PLANE_ARCHITECTURE.md).
The platform-v2 refactor (the shared Python core under `clanker/`) is described in
[ARCHITECTURE.md](ARCHITECTURE.md); the agent-backend layer in [AGENTS.md](AGENTS.md).

## Provider Architecture
`ctfvm` keeps the shared orchestration flow in `scripts/ctfvm` and pushes cloud-specific lifecycle logic into provider modules under `scripts/lib/ctfvm/providers/`.

- Shared flow: prompt generation, upload staging, auth sync, toolbox startup, tmux/session management.
- Shared transport: HTTP control-plane calls for remote exec and file transfer.
- Provider modules: instance discovery, create/delete, status/IP lookup, registry pull helpers, and VS Code host preparation.
- Current providers:
  - GCP
  - DigitalOcean

## Faster startup with a Remote Registry
Building `ctf-toolbox` from scratch on every VM is slow. The preferred path is now to push the toolbox image to a provider-native remote registry once, then have each VM pull it directly.

Provider mappings:
- GCP: Artifact Registry
- DigitalOcean: Container Registry (DOCR)

1. Build, push, and save the default image reference locally:
```bash
./scripts/ctfvm image push-registry --provider gcp
```
This will:
- build `images/ctf-toolbox`
- push it to the provider-native registry for the selected provider
- save the pushed reference in `.ctfvm/config.json` so `ctfvm start` can reuse it automatically

Default build platform is `linux/amd64` so it runs on your x86 GCP VM even when built from macOS.
Default toolbox variant is `lean` for faster build times. Use `--variant full` if you want a separate heavyweight image.

```bash
./scripts/ctfvm image push-registry --provider gcp --variant full
```

For DigitalOcean:

```bash
./scripts/ctfvm image push-registry --provider digitalocean \
  --location nyc3 \
  --repository my-ctf-registry \
  --image-name ctf-toolbox
```

2. Start runs normally on either provider:
```bash
./scripts/ctfvm start --dir ./challenge --desc "..." --ideas "..."
```
`ctfvm start` will now pull the saved remote-registry image by default for the selected `--toolbox-variant`.
You do not need to pass the full registry URL after you have pushed once with `image push-registry`.

3. Re-push after Dockerfile/tool changes:
```bash
./scripts/ctfvm image push-registry --provider gcp
```

4. Override the default image when needed without typing the full registry path:
```bash
./scripts/ctfvm start --dir ./challenge \
  --toolbox-location europe-west4 \
  --toolbox-tag lean
```

You can also override the repo or image name:

```bash
./scripts/ctfvm start --dir ./challenge \
  --toolbox-repository ctfvm \
  --toolbox-image-name ctf-toolbox \
  --toolbox-tag full
```

The full URL form still works if you want it:

```bash
./scripts/ctfvm start --dir ./challenge \
  --toolbox-image europe-west4-docker.pkg.dev/my-project/ctfvm/ctf-toolbox:lean
```

If your registry location differs from your default compute zone or region, set it on push:

```bash
./scripts/ctfvm image push-registry --provider gcp --project my-project --location europe-west4
```

Important:
- On GCP, the VM's service account needs permission to pull from Artifact Registry, typically `roles/artifactregistry.reader`.
- On DigitalOcean, `ctfvm` stages a short-lived DOCR Docker config into the VM before pulling.
- `ctfvm image push-registry` saves the pushed reference plus provider-specific repo metadata in `.ctfvm/config.json`; `ctfvm start` uses that by default for the matching provider and `--toolbox-variant`.

## Local image archive fallback
If you do not want to use a remote registry, the old tarball upload flow still works.

1. Build and cache locally:
```bash
./scripts/ctfvm image build-local
```

2. Start using the archive explicitly:
```bash
./scripts/ctfvm start --dir ./challenge --use-local-image
```

For DigitalOcean, this is the recommended path:

```bash
./scripts/ctfvm image build-local
./scripts/ctfvm start --provider digitalocean --zone nyc3 --use-local-image --dir ./challenge --desc "..." --ideas "..."
```

Notes:
- `--machine-type` applies to GCP.
- `--size-slug` and `--ssh-key <fingerprint>` apply to DigitalOcean.
- For DigitalOcean, `--zone` is the droplet region.
- `--ssh-key` is optional now unless you want SSH-based break-glass access.

## What start does
1. Creates an ephemeral VM on the selected provider with a startup script/user-data bootstrap.
2. The VM bootstrap starts the HTTP control-plane service and the local machine waits for that endpoint to become reachable.
3. Uploads challenge folder, prompt file, runner scripts, and managed Codex config through the control plane.
   - Also installs all repo skills from `./skills/` into `/workspace/.codex/skills/`.
4. Syncs local Codex OAuth session material (`~/.codex`) unless `--no-auth-sync` is set.
5. Installs managed Codex config into `/workspace/.codex/config.toml` with agent roles:
   - `exploit_tester`
   - `docs_researcher`
   - bundled `gdb` MCP server
   - optional default `ida` MCP server when `CTFVM_DEFAULT_IDA_MCP_URL` is set
6. Pulls the configured `ctf-toolbox` image from the configured remote registry by default, or loads a local archive when `--use-local-image` is set.
7. Launches `ctf-toolbox` container.
8. Starts the managed WireGuard VPN by default so the VM and container can reach local-network challenge services at their original LAN/VPN IPs.
9. Starts `tmux` session `ctf` with:
- `supervisor` window only (interactive Codex)
- no extra windows by default

Runtime defaults inside `ctf-toolbox`:
- `/workspace/.venv` is initialized before the supervisor starts.
- `python`, `pip`, and `uv` should be on `PATH` inside the container.
- Default Python packages are `pwntools` and `z3-solver`, plus `angr` on the `full` variant.
- Passwordless `sudo` is available, so Codex can install missing Debian packages directly.
- The supervisor prompt tells Codex to verify `python` and `uv` before assuming they are missing and to install dependencies itself when blocked.

## Editing prompts
Prompt text is now file-backed so you can edit behavior without patching scripts:

- Supervisor instructions:
  - `prompts/supervisor/instructions.txt`
- Legacy idea worker template (only for `ctfvm ideas`):
  - `prompts/workers/idea_worker.txt`

`ctfvm start` uploads `prompts/` into the VM run directory at `/home/ctf/run/prompts`.

## Default MCP servers
The VM seeds `/workspace/.codex/config.toml` from `images/ctf-toolbox/codex-config/config.toml`.

Defaults:
- `gdb` is pre-registered via the bundled stdio server.
- `ida` can be pre-registered as a remote HTTP MCP server by setting `CTFVM_DEFAULT_IDA_MCP_URL` before `ctfvm start`.

Example:

```bash
export CTFVM_DEFAULT_IDA_MCP_URL="http://209.38.252.191:8765/mcp"
./scripts/ctfvm start --dir ./challenge --desc "..." --ideas "..."
```

## Seamless local continuation (important CTF workflow)
- Work directly in the same remote files with VS Code Remote-SSH:
  - `./scripts/ctfvm vscode`
  - Opens `/home/ctf/run/challenge` on the VM.
- Drop to raw shell on VM for manual reversing/exploitation:
  - `./scripts/ctfvm shell`
- Bring progress local anytime:
  - `./scripts/ctfvm sync-down --out ./live-copy`
- Push your local edits back into the active VM:
  - `./scripts/ctfvm sync-up --src ./live-copy`

This supports exactly the “Codex got stuck, I continue manually, then resume Codex” loop without losing instrumentation/debug edits.

Important:
- These continuation paths are still SSH-based.
- The routine automation path is now HTTP control-plane based, so SSH is no longer in the hot path for every UI poll or remote command.

## Bundled debugger MCP
- CTFVM now includes a bundled local `gdb` MCP server inside `ctf-toolbox`.
- It is pre-registered in the managed Codex config as server name `gdb`.
- A matching skill is installed into `/workspace/.codex/skills/` as `$gdb-mcp`.

Example:

```text
Use $gdb-mcp.
Start GDB on /workspace/challenge/chal, break at main, run it, and inspect registers after the crash.
```

## Seeing Codex work live
- `./scripts/ctfvm chat`: direct interactive Codex TUI (`supervisor` window), closest to Claude Code style.
- `./scripts/ctfvm attach`: full tmux session (supervisor plus any windows you explicitly started).
- `./scripts/ctfvm monitor`: read-only live dashboard in terminal:
  - current tmux windows
  - last 120 lines from `supervisor` pane
- Codex now runs with `--no-alt-screen` in supervisor/workers so output is easier to follow in tmux and monitor mode.
- Supervisor starts with multi-agent enabled and should spawn subagents only when needed.

## Subagents (supervisor-native)
Use this when multiple exploit paths or documentation questions should be handled in parallel by Codex-native subagents.

In supervisor chat, invoke:

```text
Use $ctf-exploit-subagent.
Spawn exploit_tester subagents for these hypotheses: ...

Use $ctf-docs-subagent.
Spawn docs_researcher for this command behavior question: ...
```

The two skills define separate subagent roles:
- `exploit_tester`: test one exploit hypothesis, produce verdict and evidence.
- `docs_researcher`: answer docs/behavior questions with source + minimal verification command.

Recommended pattern:
- Use one `exploit_tester` per independent hypothesis or challenge component.
- Use `docs_researcher` whenever command semantics or platform behavior are uncertain.
- Merge only evidence-backed exploit results into the final chain.

When subagents are spawned, `subagent-tmux-bridge.sh` auto-creates tmux sessions:
- session name: `subagent-<agent-id>`
- default window target: `subagent-<agent-id>:0`

You can steer a mirrored subagent session from CLI:

```bash
./scripts/ctfvm send --target subagent-<agent-id>:0 --text "continue with payload B and report quickly"
```

For blind callbacks and outbound-connectivity probes:

```text
Use $webhook-site-callbacks to set up webhook.site callback probes for this target.
```

If the VM run started before this feature existed, sync the skill into the running VM:

```bash
./scripts/ctfvm sync-skill --skill ctf-exploit-subagent
./scripts/ctfvm sync-skill --skill ctf-docs-subagent
./scripts/ctfvm sync-skill --skill webhook-site-callbacks
```

## Recommended interface setup
- Terminal-first (Claude Code-like): `ctfvm chat`
- Manual intervention shell: `ctfvm shell`
- Remote MCP setup: `ctfvm mcp add ida --url http://10.1.1.8:8080/mcp`
- GUI workflow: `ctfvm vscode` (Remote-SSH workspace on VM)
- Fleet overview and artifact browsing: `ctfvm ui`

## Remote MCP servers
Codex CLI already supports remote MCP servers via `codex mcp add --url ...`. `ctfvm` now wraps that so you can configure the active VM/container directly from your local machine.

Examples:

```bash
./scripts/ctfvm mcp add ida --url http://10.1.1.8:8080/mcp
./scripts/ctfvm mcp list
./scripts/ctfvm mcp get ida --json
./scripts/ctfvm mcp remove ida
```

If the supervisor session is already running and you want it to pick up the new MCP config immediately:

```bash
./scripts/ctfvm mcp --restart-supervisor add ida --url http://10.1.1.8:8080/mcp
```

Important:
- The MCP URL is resolved from inside the VM/container, not from your laptop.
- If `10.1.1.8` is only reachable on your local LAN or VPN, keep the managed CTFVM VPN enabled so the VM can route to it.
- `ctfvm mcp` is a thin wrapper around `codex mcp`, so standard Codex flags like `--url` and `--bearer-token-env-var` still work.

## Local-network VPN
CTFVM starts a WireGuard VPN automatically by default. The VM routes RFC1918 local-network ranges through your laptop, and the laptop enables forwarding plus NAT so challenge services can reply without custom routes on your LAN.

Default routed CIDRs:
- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`

Start with defaults:

```bash
./scripts/ctfvm start --dir ./challenge --desc "..."
```

Customize or disable:

```bash
./scripts/ctfvm start --dir ./challenge --vpn-local-cidr 192.168.56.0/24
./scripts/ctfvm start --dir ./challenge --vpn-local-cidrs "10.10.0.0/16,192.168.1.0/24"
./scripts/ctfvm start --dir ./challenge --no-vpn
```

Manage an existing run:

```bash
./scripts/ctfvm vpn status
./scripts/ctfvm vpn up --local-cidr 192.168.56.0/24
./scripts/ctfvm vpn down
```

Generated files:
- Local VPN state and configs: `.ctfvm/vpn/<run-id>/`
- Remote VPN env file: `/home/ctf/run/vpn/<interface>.env`
- Remote VPN state: `/home/ctf/run/vpn/<interface>.json`

Operational notes:
- Local prerequisites are `wg`, `wg-quick`, `sudo`, `jq`, and `base64`; Linux NAT additionally needs `iptables`.
- `ctfvm start` may ask for local sudo so `wg-quick` can create the interface and NAT rules. When launched from the web UI, run `sudo -v` in a terminal first or start with `--no-vpn`, because background UI jobs cannot show a password prompt.
- GCP runs create a UDP firewall rule for the WireGuard port, default `51820`.
- DigitalOcean droplets are assumed publicly reachable unless you attach your own cloud firewall; if you do, allow the WireGuard UDP port.
- Set `CTFVM_VPN=0` or pass `--no-vpn` to skip automatic VPN startup.
- Set `CTFVM_VPN_CIDRS` or pass `--vpn-local-cidrs` to change the default routes.
- Set `CTFVM_VPN_EGRESS_IF` if automatic local egress-interface detection picks the wrong interface.

## Legacy TCP bridges
`ctfvm bridge` remains available for older TCP-only workflows, but the preferred path is the managed VPN above.

Example config:

```json
{
  "targets": [
    {
      "name": "main",
      "internal_host": "10.20.30.40",
      "internal_port": 31337,
      "vm_port": 31337
    }
  ]
}
```

Bring the bridge up from your laptop:

```bash
./scripts/ctfvm bridge up --config ./bridge.json
./scripts/ctfvm bridge status
./scripts/ctfvm bridge list
```

Your laptop initiates the SSH session to the VM, so the laptop does not need to be publicly reachable. The VM/container then reaches the bridged service at `host.docker.internal:<vm_port>`.

Generated files:
- Local bridge state: `.ctfvm/bridges/<run-id>/<name>.json`
- Remote bridge state: `/home/ctf/run/bridge/<name>.json`
- Remote bridge env file: `/home/ctf/run/bridge/<name>.env`

Example env file entries inside the VM:
- `CTF_BRIDGE_MAIN_HOST=host.docker.internal`
- `CTF_BRIDGE_MAIN_PORT=31337`

Important:
- TCP only. UDP and raw-socket workflows still need a VPN or routed overlay.
- The bridge requires a VM started with the current `ctfvm` version so `ctf-toolbox` includes `host.docker.internal`.
- Nothing is exposed on the laptop; the data path is an outbound SSH connection from laptop to VM plus host-local relays on the VM.

## Web GUI (`ctfvm ui`)
Launch a lightweight local web dashboard:

```bash
./scripts/ctfvm ui --host 127.0.0.1 --port 8765
```

Then open:
- `http://127.0.0.1:8765`

The UI shows:
- multi-run fleet overview across providers
- focused live pane output with tab-style switching between tmux windows
- findings and supervisor tails for the selected run
- tmux window list across all tmux sessions on the VM user (`ctf`, plus subagent sessions)
- artifact file listing
- inject input box for steering the run

Transport notes:
- the UI prefers the VM HTTP control plane for snapshot polling and artifact access
- older runs without control-plane metadata can still fall back to SSH-backed behavior

## Python core (`clanker`) — platform-v2

The shared Python core under `clanker/` is being grown alongside the bash CLI (a strangler
migration — see [REFACTOR_PLAN.md](REFACTOR_PLAN.md)). Run it with `python -m clanker <cmd>` from the
repo root. Available today:

Configure everything in one place: copy [`.env.example`](../.env.example) to `.env` and edit. Every
knob is `CTFVM_*` and listed there; `python -m clanker config show` prints the resolved value and where
each came from (cli / .env / env / secret / config.json / default).

```bash
python -m clanker config show                          # effective config + provenance
python -m clanker serve --host 127.0.0.1 --port 8765   # the new web UI / /api/v1 server
python -m clanker runs [--json]                        # list known runs from .ctfvm/
python -m clanker status --run-id <id>                 # one run's status
python -m clanker cleanup-state [--dry-run] [--prune-non-running]
python -m clanker fetch --run-id <id> [--out DIR]      # download findings/artifacts/logs
python -m clanker sync-down --run-id <id> [--out DIR]
python -m clanker auth claude [--token <t>] [--name <profile>]   # Claude OAuth token (global or per-profile)
python -m clanker auth codex --name <profile> [--api-key K | --codex-home DIR]
python -m clanker auth show                            # which backends have credentials
python -m clanker config profiles                      # list credential profiles
python -m clanker stage-agent --agent codex|claude-code --staging-dir DIR [--account <profile>]
python -m clanker share                                # expose the UI via ngrok with a tokenized link
python -m clanker render-agent-config --agent codex|claude-code
```

**Multiple subscriptions (credential profiles).** Store one credential set per account and select it
per run. Both backends are supported per-account — Claude via a per-profile OAuth token, Codex via a
per-profile `OPENAI_API_KEY` *or* a per-account `~/.codex` dir (its `auth.json` is portable). A profile
overlays config just below CLI flags.

```bash
python -m clanker auth claude --name alice                 # alice's Claude subscription
python -m clanker auth codex  --name bob --api-key sk-...   # bob's Codex API account
python -m clanker auth codex  --name team --codex-home ~/.codex-team
python -m clanker stage-agent --agent claude-code --account alice --staging-dir /tmp/run
```

**Sharing the UI (`clanker share`).** For temporary CTF VMs you can expose the dashboard via ngrok with
a one-click tokenized link. `share` mints an ephemeral UI token (or uses `CTFVM_UI_TOKEN`), starts the
**authed** server, launches ngrok, and prints `https://<public>/?token=<token>`. The `?token` sets a
cookie on first open, so the rest of the session just works. With no token configured, `clanker serve`
stays open and local-only (127.0.0.1) as before.

`ctfvm cleanup-state` already delegates to the core. The UI server (`serve`) speaks the versioned
`/api/v1/*` surface documented in [API.md](API.md). `start`/`destroy` and interactive break-glass
(`attach`/`shell`/`vscode`) remain in the bash CLI until the provisioning port lands.

The selectable **agent backend** (`--agent codex|claude-code`) is the platform-v2 headline: Claude Code
authenticates via a `claude setup-token` OAuth token (stored by `clanker auth claude`), never by
copying credential files. See [AGENTS.md](AGENTS.md).

## Security notes
- No API keys are required.
- OAuth session data copied to VM is permission-restricted and wiped on `destroy`.
- VM auto self-destructs after timeout (`--timeout-min`, default 1440).
- The control plane should be restricted with provider firewalls or private networking.
- Open internet is enabled by default unless you place the workers inside a tighter VPC topology.

## Known constraints
- First run is slow due to apt install and container build.
- Python packages (`pwntools`, `z3-solver`, and `angr` for `full`) install on container start instead of being baked into the image.
- Local cache upload can still be large, but usually much faster than rebuilding all packages remotely.
- Codex CLI package install on Linux is best-effort (`@openai/codex` first, then `codex-cli`).
- VM self-delete needs default service account permissions to delete its own instance. If denied, VM shuts down but may still require manual deletion.
- Interactive break-glass commands like `attach`, `shell`, and `vscode` still depend on SSH being available.

## TODOs

1. Allow user to manually spawn a new codex session from the UI with prompt. This can be done by calling a bash script to spawn a new codex cli session.
2. Support for other AI providers, e.g. claude code, gemini cli or opencode
3. Support for other cloud providers
4. Support for running locally
5. Support for deploying to VPS running Incus
6. Faster VM deployment times (by putting image on cloud container registry)
7. View subagent work in separate tmux terminal 
