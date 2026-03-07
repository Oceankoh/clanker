---
name: ctf-idea-workers
description: Spawn tmux-based Codex idea workers for CTFVM runs, one window per exploitation hypothesis. Use when users want multiple attack paths explored in parallel and still need live steering of each worker window.
---

# CTF Idea Workers

Run this workflow on the VM host shell (not inside the container prompt) to fan out hypothesis testing into tmux windows.

## Preconditions
- Confirm `/home/ctf/run/spawn-idea-workers.sh` exists and is executable.
- Confirm tmux session `ctf` exists.
- Confirm `ctf-toolbox` container is running with Codex available.

## Execute
1. Build a concrete idea list from user input or current findings.
2. Keep each idea single-purpose and testable.
3. Run the launcher:

```bash
/home/ctf/run/spawn-idea-workers.sh --run-dir /home/ctf/run --ideas "heap unlink via free list; format string in logger path"
```

Use a file for longer lists:

```bash
/home/ctf/run/spawn-idea-workers.sh --run-dir /home/ctf/run --ideas-file /home/ctf/run/ideas.txt
```

Optional model override:

```bash
/home/ctf/run/spawn-idea-workers.sh --run-dir /home/ctf/run --ideas-file /home/ctf/run/ideas.txt --model gpt-5.3-codex
```

## Inspect Outputs
- Worker logs: `/workspace/logs/worker-idea-*.log`
- Worker artifacts: `/workspace/artifacts/ideas/idea-*/`
- Worker windows: `ctf:idea-001`, `ctf:idea-002`, ...
- Steer a worker: `ctfvm send --target ctf:idea-001 --text "..."`

## Report Back
- Summarize which hypotheses succeeded, failed, or remain ambiguous.
- Link each conclusion to a concrete artifact path.
- Propose next hypotheses only when evidence is inconclusive.

## Failure Handling
- If launcher exits non-zero, inspect tmux session state and the newest `worker-idea-*.log`.
- If the launcher is missing, ask to sync updated runner scripts before retrying.
