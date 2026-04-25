---
name: gdb-mcp
description: Use the bundled persistent GDB MCP server in CTFVM for interactive debugging. Trigger this when you need a stateful GDB session across multiple tool calls, such as setting breakpoints, stepping, inspecting registers or memory, attaching to remote targets, or interrupting and resuming execution cleanly.
---

# GDB MCP

Use this skill inside CTFVM when debugging requires persistent GDB state.
Prefer the MCP tools over driving `gdb` through a shell when you need multiple debugger interactions in one session.

## Tools

- `gdb_start`: start a session, optionally with `binary`, `remote`, `args`, and `init_script`
- `gdb_exec`: send normal GDB commands; use command `interrupt` if the target is running
- `gdb_stop`: terminate the active session when done

## Workflow

1. Start once with the right binary and optional remote target.
2. Use `gdb_exec` for breakpoints, run/continue, backtraces, register inspection, disassembly, and memory reads.
3. If a long-running command times out, send `interrupt` through `gdb_exec`.
4. Stop the session when finished or before switching to a different target.

## Examples

```text
Use $gdb-mcp.
Start GDB on /workspace/challenge/chal, break at main, run it with stdin from /workspace/input.txt, then show registers and a backtrace.
```

```text
Use $gdb-mcp.
Connect to a QEMU gdbstub on localhost:1234 with symbols from /workspace/challenge/vmlinux, inspect the current RIP, and disassemble around it.
```

## Rules

- Keep one debugger session scoped to one target at a time.
- Prefer exact GDB commands and report the key output succinctly.
- When the debugger state matters, do not restart unless necessary.
- If you need a richer init script such as GEF and it already exists on disk, pass it with `init_script`.
