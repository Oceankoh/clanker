#!/usr/bin/env python3
"""Minimal stdio MCP server for persistent GDB sessions."""

import asyncio
import json
import shlex
import signal
import sys

PROMPT = "(gdb-mcp) "
SERVER_NAME = "gdb"
SERVER_VERSION = "0.1.0"

TOOLS = [
    {
        "name": "gdb_start",
        "description": (
            "Start a new GDB session.\n\n"
            "binary: path to ELF binary or vmlinux to load symbols from\n"
            'remote: GDB remote target (e.g. "localhost:1234" for QEMU -s)\n'
            "args: additional GDB CLI flags\n"
            "init_script: path to a GDB Python script to source at startup\n"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "binary": {"type": "string", "default": "", "title": "Binary"},
                "remote": {"type": "string", "default": "", "title": "Remote"},
                "args": {"type": "string", "default": "", "title": "Args"},
                "init_script": {
                    "type": "string",
                    "default": "",
                    "title": "Init Script",
                },
            },
            "title": "gdb_startArguments",
        },
    },
    {
        "name": "gdb_exec",
        "description": (
            "Send a command to GDB and return the output.\n\n"
            'command: any GDB command (e.g. "break main", "bt", "x/16gx $rsp")\n'
            'timeout: max seconds to wait; use "interrupt" to stop a running target\n'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "title": "Command"},
                "timeout": {"type": "number", "default": 30.0, "title": "Timeout"},
            },
            "required": ["command"],
            "title": "gdb_execArguments",
        },
    },
    {
        "name": "gdb_stop",
        "description": "Terminate the current GDB session.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "title": "gdb_stopArguments",
        },
    },
]


class GDBSession:
    """Manage a persistent GDB subprocess."""

    def __init__(self):
        self.proc = None

    @property
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    async def start(self, cmd: list[str]) -> str:
        if self.alive:
            await self.stop()
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        startup = await self._read_until("(gdb) ")
        await self._send(f"set prompt {PROMPT}")
        await self._read_until(PROMPT)
        for setup in (
            "set pagination off",
            "set confirm off",
            "set width 0",
        ):
            await self._send(setup)
            await self._read_until(PROMPT)
        return startup.strip()

    async def execute(self, command: str, timeout: float = 30.0) -> str:
        if not self.alive:
            raise RuntimeError("No active GDB session. Call gdb_start first.")
        await self._send(command)
        try:
            output = await asyncio.wait_for(
                self._read_until(PROMPT), timeout=timeout
            )
        except asyncio.TimeoutError:
            return (
                f"[Timed out after {timeout}s. "
                "Target may be running - send 'interrupt' to stop.]"
            )
        return output.strip()

    async def interrupt(self) -> str:
        if not self.alive:
            raise RuntimeError("No active GDB session.")
        self.proc.send_signal(signal.SIGINT)
        try:
            output = await asyncio.wait_for(
                self._read_until(PROMPT), timeout=5.0
            )
        except asyncio.TimeoutError:
            return "[Interrupt sent but no response within 5s.]"
        return output.strip()

    async def _send(self, text: str):
        self.proc.stdin.write(f"{text}\n".encode())
        await self.proc.stdin.drain()

    async def _read_until(self, marker: str) -> str:
        buf = b""
        encoded = marker.encode()
        while True:
            chunk = await self.proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            if encoded in buf:
                idx = buf.index(encoded)
                return buf[:idx].decode(errors="replace")
        return buf.decode(errors="replace")

    async def stop(self):
        if not self.alive:
            return
        try:
            self.proc.stdin.write(b"quit\ny\n")
            await self.proc.stdin.drain()
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError, BrokenPipeError):
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        self.proc = None


session = GDBSession()


async def gdb_start(
    binary: str = "",
    remote: str = "",
    args: str = "",
    init_script: str = "",
) -> str:
    cmd = ["gdb", "-q", "-nx"]
    if args:
        cmd.extend(shlex.split(args))
    if binary:
        cmd.append(binary)

    startup = await session.start(cmd)
    parts = [startup] if startup else []

    if init_script:
        await session._send(f"source {init_script}")
        try:
            await asyncio.wait_for(
                session._read_until("settings"), timeout=180
            )
        except asyncio.TimeoutError:
            parts.append("[init_script load timed out]")
        else:
            parts.append("[init_script loaded]")

        await asyncio.sleep(2)
        for fix_cmd in (
            "python gdb.prompt_hook = None",
            f"set prompt {PROMPT}",
            "gef config gef.disable_color True",
            "gef config context.enable False",
        ):
            await session._send(fix_cmd)
            try:
                await asyncio.wait_for(
                    session._read_until(PROMPT), timeout=10
                )
            except asyncio.TimeoutError:
                pass

    if remote:
        out = await session.execute(f"target remote {remote}", timeout=10)
        parts.append(out)

    return "\n".join(parts) or "GDB session started."


async def gdb_exec(command: str, timeout: float = 30.0) -> str:
    if command.strip().lower() == "interrupt":
        return await session.interrupt()
    return await session.execute(command, timeout=timeout)


async def gdb_stop() -> str:
    if not session.alive:
        return "No active session."
    await session.stop()
    return "GDB session terminated."


async def call_tool(name: str, arguments: dict) -> str:
    if name == "gdb_start":
        return await gdb_start(
            binary=str(arguments.get("binary", "")),
            remote=str(arguments.get("remote", "")),
            args=str(arguments.get("args", "")),
            init_script=str(arguments.get("init_script", "")),
        )
    if name == "gdb_exec":
        return await gdb_exec(
            command=str(arguments.get("command", "")),
            timeout=float(arguments.get("timeout", 30.0)),
        )
    if name == "gdb_stop":
        return await gdb_stop()
    raise RuntimeError(f"Unknown tool: {name}")


def send(message: dict):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def send_result(request_id, result: dict):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def send_error(request_id, code: int, message: str):
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


async def handle_message(message: dict):
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        protocol_version = params.get("protocolVersion", "2025-06-18")
        send_result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            },
        )
        return

    if method == "notifications/initialized":
        return

    if method == "ping":
        send_result(request_id, {})
        return

    if method == "tools/list":
        send_result(request_id, {"tools": TOOLS})
        return

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = await call_tool(tool_name, arguments)
        except Exception as exc:
            send_result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        else:
            send_result(
                request_id,
                {
                    "content": [{"type": "text", "text": result}],
                    "isError": False,
                },
            )
        return

    if request_id is not None:
        send_error(request_id, -32601, f"Method not found: {method}")


async def main():
    while True:
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            break
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        await handle_message(message)

    if session.alive:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
