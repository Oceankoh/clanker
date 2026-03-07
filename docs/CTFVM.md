# Disposable CTF VM with Codex CLI

## Prerequisites
- `gcloud` installed and authenticated (`gcloud auth login`)
- Default project and zone configured or passed to `ctfvm start`
- Local Codex login complete (`codex login`)

## Commands
From repo root:

```bash
./scripts/ctfvm start --dir ./challenge --desc "..." --ideas "..."
./scripts/ctfvm attach
./scripts/ctfvm chat
./scripts/ctfvm shell
./scripts/ctfvm vscode
./scripts/ctfvm monitor
./scripts/ctfvm ui
./scripts/ctfvm inject --msg "Try the heap unlink path"
./scripts/ctfvm ideas --ideas "heap unlink candidate; tcache poisoning fallback"
./scripts/ctfvm sync-skill --skill ctf-idea-workers
./scripts/ctfvm sync-skill --skill webhook-site-callbacks
./scripts/ctfvm sync-down --out ./live-copy
./scripts/ctfvm sync-up --src ./live-copy
./scripts/ctfvm logs
./scripts/ctfvm fetch --out ./outputs
./scripts/ctfvm destroy
```

## Optional add-ins inside `ctf-toolbox`
Use the bundled add-in scripts when you need heavier toolsets without making every VM startup slower.

1. Attach to the VM and keep `ctf-toolbox` running (`./scripts/ctfvm attach`).
2. Install one or more profiles from your local machine:

```bash
gcloud compute ssh <instance> --zone <zone> --project <project> \
  --command "sudo -u ctf docker exec -u root ctf-toolbox bash -lc '/opt/ctf-toolbox/addons/install-common-addins.sh crypto'"
```

Or run directly on the VM shell:

```bash
sudo -u ctf docker exec -u root ctf-toolbox bash -lc '/opt/ctf-toolbox/addons/install-common-addins.sh cloud forensics'
```

Profiles:
- `crypto`: cracking and crypto helpers (`hashcat`, `john`, `hcxtools`, `steghide`, `outguess`).
- `cloud`: cloud/Kubernetes helpers (`awscli`, `kubectl`) and cloud recon packages (`ScoutSuite`, `trufflehog`).
- `forensics`: DFIR/file-carving tooling (`sleuthkit`, `testdisk`, `foremost`, `binwalk`, `exiftool`, `tshark`, `yara`, `volatility3`).
- `pentest`: recon/pentest tooling (`nmap`, `ffuf`, `sqlmap`), kept separate from core image.
- `heavy`: slower, heavyweight CTF stack (`sagemath`, best-effort `radare2`, `angr`).
- `all`: installs default non-pentest profiles in one pass.

## Faster startup with local image cache (recommended)
Building `ctf-toolbox` from scratch on every VM is slow. You can keep the image archive locally and load it into each disposable VM.

1. Build and cache locally once:
```bash
./scripts/ctfvm image build-local
```
This creates `.ctfvm/cache/ctf-toolbox.tar.gz`.
Default build platform is `linux/amd64` so it runs on your x86 GCP VM even when built from macOS.
Default toolbox variant is `lean` for faster build times. Use `--variant full` if you want heavyweight tools baked in.

```bash
./scripts/ctfvm image build-local --variant full
```

2. Start runs using the local cached image:
```bash
./scripts/ctfvm start --dir ./challenge --desc "..." --ideas "..." --use-local-image
```
If the default archive exists, `start` will auto-use it even without `--use-local-image`.

3. Rebuild cache after Dockerfile/tool changes:
```bash
./scripts/ctfvm image build-local
```

4. Force on-VM rebuild when needed:
```bash
./scripts/ctfvm start --dir ./challenge --force-remote-build
```

To force on-VM rebuild with heavyweight tools:

```bash
./scripts/ctfvm start --dir ./challenge --force-remote-build --toolbox-variant full
```

## What start does
1. Creates an ephemeral GCP VM with a startup script.
2. Uploads challenge folder, prompt file, runner scripts, and toolbox Docker context.
   - Also installs all repo skills from `./skills/` into `/workspace/.codex/skills/`.
3. Syncs local Codex OAuth session material (`~/.codex`) unless `--no-auth-sync` is set.
4. Builds and launches `ctf-toolbox` container.
5. Starts `tmux` session `ctf` with:
- `supervisor` window only (interactive Codex)
- no extra windows by default

## Editing prompts
Prompt text is now file-backed so you can edit behavior without patching scripts:

- Supervisor instructions:
  - `prompts/supervisor/instructions.txt`
- Idea worker template:
  - `prompts/workers/idea_worker.txt`

`ctfvm start` uploads `prompts/` into the VM run directory at `/home/ctf/run/prompts`.

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

## Seeing Codex work live
- `./scripts/ctfvm chat`: direct interactive Codex TUI (`supervisor` window), closest to Claude Code style.
- `./scripts/ctfvm attach`: full tmux session (supervisor plus any windows you explicitly started).
- `./scripts/ctfvm monitor`: read-only live dashboard in terminal:
  - current tmux windows
  - last 120 lines from `supervisor` pane
- Codex now runs with `--no-alt-screen` in supervisor/workers so output is easier to follow in tmux and monitor mode.
- Subagents should be spawned from inside the running Codex session only when needed.

## Idea workers (supervisor -> worker codex exec)
Use this when you want multiple exploitation hypotheses tested in parallel with one command:

```bash
./scripts/ctfvm ideas --ideas "format string in logger; heap unlink in delete path; race in session rotate"
```

Or pass a local ideas file (one idea per line; `#` comments allowed):

```bash
./scripts/ctfvm ideas --ideas-file ./ideas.txt --model gpt-5.3-codex
```

Outputs:
- tmux windows: `ctf:idea-001`, `ctf:idea-002`, ...
- per-worker logs: `/home/ctf/run/logs/worker-idea-*.log`
- worker artifacts: `/home/ctf/run/artifacts/ideas/idea-*/`
- steering command: `./scripts/ctfvm send --target ctf:idea-001 --text "..."`

Compatibility note: `--parallel` is accepted but currently ignored in tmux mode (one window per idea).

In supervisor chat, you can explicitly invoke the skill:

```text
Use $ctf-idea-workers to test these hypotheses in parallel: ...
```

For blind callbacks and outbound-connectivity probes:

```text
Use $webhook-site-callbacks to set up webhook.site callback probes for this target.
```

If the VM run started before this feature existed, sync the skill into the running VM:

```bash
./scripts/ctfvm sync-skill --skill ctf-idea-workers
./scripts/ctfvm sync-skill --skill webhook-site-callbacks
```

## Recommended interface setup
- Terminal-first (Claude Code-like): `ctfvm chat`
- Manual intervention shell: `ctfvm shell`
- GUI workflow: `ctfvm vscode` (Remote-SSH workspace on VM)

## Web GUI (`ctfvm ui`)
Launch a lightweight local web dashboard:

```bash
./scripts/ctfvm ui --host 127.0.0.1 --port 8765
```

Then open:
- `http://127.0.0.1:8765`

The UI shows:
- tmux window list
- live supervisor pane output
- idea-worker log tails (`worker-idea-*` when present)
- inject queue tail
- artifact file listing
- inject input box for steering the run

## Security notes
- No API keys are required.
- OAuth session data copied to VM is permission-restricted and wiped on `destroy`.
- VM auto self-destructs after timeout (`--timeout-min`, default 1440).
- Open internet is enabled by default.

## Known constraints
- First run is slow due to apt install and container build.
- Python packages (`pwntools`, `z3-solver`, and `angr` for `full`) install on container start instead of being baked into the image.
- Local cache upload can still be large, but usually much faster than rebuilding all packages remotely.
- Codex CLI package install on Linux is best-effort (`@openai/codex` first, then `codex-cli`).
- VM self-delete needs default service account permissions to delete its own instance. If denied, VM shuts down but may still require manual deletion.
