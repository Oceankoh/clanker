# ctf-toolbox image

This image is built on the VM at run start.
Default variant is `lean` to reduce build time. A `full` variant is available when needed.

Included categories:
- Networking basics: `curl`, `tcpdump`
- RE/pwn basics: `gdb`, `strace`, `binutils`
- Python tooling: `python3`, `python3-venv`, `uv`, plus a preinitialized `/workspace/.venv` with `pwntools` and `z3-solver`
- AI client runtime: `nodejs` and `npm`; the `codex` CLI itself is installed on the VM at boot into `/opt/ctfvm/npm-global` and mounted into the container so each new VM picks up the latest npm release
- Local MCP helpers: bundled `gdb` stdio MCP server at `/opt/ctf-toolbox/mcp/gdb_mcp.py`

The image also includes baked Codex role config templates under:
- `/opt/ctf-toolbox/codex-config/config.toml`
- `/opt/ctf-toolbox/codex-config/roles/*.toml`
- `/opt/ctf-toolbox/mcp/gdb_mcp.py`

CTFVM installs these templates into `/workspace/.codex/` so supervisor sessions have consistent role definitions (`exploit_tester`, `docs_researcher`).
The managed Codex config also pre-registers the bundled `gdb` MCP server, so in-VM Codex sessions can use it without manual setup.
If `CTFVM_DEFAULT_IDA_MCP_URL` is set when `ctfvm start` runs, the managed config also pre-registers an `ida` HTTP MCP server at that URL.
Supervisor prompts explicitly tell Codex to check for `python` and `uv` before assuming they are missing and to install dependencies directly when blocked.

`full` variant adds `sagemath` and best-effort `radare2`, and installs `angr` at container start.

The default user is non-root (`ctf`), but passwordless `sudo` is enabled so Codex can install system packages when needed:

```bash
sudo apt-get update && sudo apt-get install -y <package>
```

For Python packages, prefer:

```bash
uv pip install --python /workspace/.venv/bin/python <package>
```

Optional proprietary tools are not bundled. Mount them under `/opt/licensed` on the VM and invoke manually.
