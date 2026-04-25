# HTTP Control-Plane Architecture

## What changed

The remote execution path no longer depends on opening a fresh SSH session for every operation.

Each worker VM now boots a small HTTP control-plane service that can:

- execute shell commands
- receive tar uploads for challenge state and runner assets
- receive file uploads
- stream file downloads back to the local machine

`./scripts/ctfvm` and the local web UI both use that control plane first. Older runs without control-plane metadata still fall back to SSH where possible.

## Component map

Local side:
- `scripts/ctfvm`: orchestration, run state, provider selection
- `scripts/ctfvm_control_client.py`: HTTP client used by the CLI
- `scripts/ctfvm_ui/*`: local fleet dashboard and run monitor

VM side:
- `vm/startup.sh`: bootstrap and service installation
- `vm/control_server.py`: HTTP wrapper around shell exec and file transfer
- `ctf-toolbox` container: challenge runtime
- `tmux` session `ctf`: supervisor and optional subagents

Cloud side:
- provider lifecycle adapters in `scripts/lib/ctfvm/providers/*`
- optional static network layer in `infra/terraform/*`

## Request flow

Provisioning flow:
1. The local CLI asks the provider adapter to create a VM.
2. The startup script installs the control-plane service and starts it with per-run credentials.
3. The local CLI waits for `http://<vm-ip>:<port>/healthz`.
4. Assets and commands are pushed over HTTP until the toolbox container and supervisor are running.

Steady-state flow:
1. The local UI requests a run snapshot.
2. The UI service executes a remote shell script through the control plane.
3. That remote script reads tmux output, `findings.md`, artifacts, and status markers.
4. The UI renders one focused output stream plus findings and artifact views.

Break-glass flow:
- `attach`, `shell`, and `vscode` still use SSH intentionally because they are interactive human-operated workflows, not high-frequency polling paths.

## Security model

The control plane is designed to be gated in two layers:

1. HTTP Basic Auth with per-run credentials written into local run state.
2. Network-level filtering so only approved source CIDRs can hit the control-plane port.

For local-first usage, the practical baseline is:

- expose the worker on a public IP
- restrict ingress with provider firewalls to your workstation or office CIDR
- keep SSH enabled only for break-glass access

For tighter setups, put the workers and any companion services inside a dedicated VPC and route the aggregator from inside that network.

Practical recommendation:
- if the aggregator stays on your laptop, restrict ingress to your public IP or office CIDR
- if you later host the aggregator in the cloud, move the workers behind private networking and narrow firewall rules to that service

## Terraform split

Terraform should own the static, shared layer:

- VPC / subnet
- firewall rules
- stable tags used for worker attachment

The CLI should keep owning the dynamic layer:

- create one VM per challenge run
- inject prompts and challenge files
- start or destroy workers on demand

That split avoids Terraform state churn for ephemeral CTF workers while still making the network perimeter explicit and repeatable.

The new base stacks live under `infra/terraform/`:

- `infra/terraform/gcp`
- `infra/terraform/digitalocean`

Their outputs map directly into repo env vars consumed by `./scripts/ctfvm start`.

This is the intended division of responsibility:
- Terraform: durable network perimeter
- `ctfvm`: disposable run lifecycle

## Multi-service VPC story

This layout is meant to support extra services inside the same network later, for example:

- an IDA MCP endpoint
- internal-only challenge service bridges
- a hosted fleet aggregator instead of the local UI

The worker bootstrap still supports Codex MCP configuration, so VPC-local MCP endpoints can be injected without redesigning the run lifecycle.

That makes the architecture a better fit for your stated next step of adding extra services like an IDA MCP inside the same network boundary.

## Web UI shape

The local web UI remains the fleet aggregator.

It now benefits from the lower-latency control-plane transport while keeping:

- `findings.md` tail visibility
- artifact browsing and preview
- multi-run overview
- direct tmux steering

The run monitor was also rebalanced toward a single focused output stream, which matches the stronger output readability from the reference UI while preserving the existing findings and artifact workflow.

## Load balancer decision

A load balancer is not required for the local-first workflow because the local UI can talk directly to each worker.

Add one only if you later:

- move the aggregator into the cloud
- need TLS termination in front of private workers
- want a single stable hostname for hosted operators

For the current design, provider firewall rules plus direct worker addressing keep the system simpler.
