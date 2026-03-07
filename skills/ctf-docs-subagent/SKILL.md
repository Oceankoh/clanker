---
name: ctf-docs-subagent
description: Spawn a dedicated documentation and behavior-validation subagent in CTFVM. Use when command semantics, API behavior, platform details, or tooling usage are uncertain and must be verified before exploit steps continue.
---

# CTF Docs Subagent

Use this skill from the supervisor Codex session in CTFVM to spawn focused docs-and-verification subagents.
Spawn role: `docs_researcher`.

## Spawn Template

```text
You are docs_researcher.
Question: <documentation or behavior question>
Scope:
- Find authoritative docs or built-in tool help.
- Run a minimal local command test when possible.
- Store notes under /workspace/artifacts/subagents/docs-researcher/<label>/.
Return format:
1) answer: concise actionable answer
2) source: doc URL or command help used
3) verification: exact command run and observed behavior
4) recommended-action: how supervisor should proceed
```

## Execution Rules

- Prefer official docs, built-in `--help`, and reproducible command checks.
- Treat unverified claims as tentative.
- Return concrete usage guidance the supervisor can apply immediately.
- Log key conclusions in `/workspace/findings.md`.
