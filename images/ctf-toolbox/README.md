# ctf-toolbox image

This image is built on the VM at run start.
Default variant is `lean` to reduce build time. A `full` variant is available when needed.

Included categories:
- Networking basics: `curl`, `tcpdump`
- RE/pwn basics: `gdb`, `strace`, `binutils`
- Python tooling (installed at container start): `pwntools`, `z3-solver`
- AI client: `codex` CLI (OAuth session synced under `/workspace/.codex`)

The image also includes baked Codex role config templates under:
- `/opt/ctf-toolbox/codex-config/config.toml`
- `/opt/ctf-toolbox/codex-config/roles/*.toml`

CTFVM installs these templates into `/workspace/.codex/` so supervisor sessions have consistent role definitions (`exploit_tester`, `docs_researcher`).

`full` variant adds `sagemath` and best-effort `radare2`, and installs `angr` at container start.

## Optional add-ins
The image includes profile installers under `/opt/ctf-toolbox/addons`:

- `install-common-addins.sh crypto`
- `install-common-addins.sh cloud`
- `install-common-addins.sh forensics`
- `install-common-addins.sh pentest`
- `install-common-addins.sh heavy`
- `install-common-addins.sh all`

The default user is non-root (`ctf`), but passwordless `sudo` is enabled so Codex can install system packages when needed:

```bash
sudo apt-get update && sudo apt-get install -y <package>
```

You can still run installers as root directly:

```bash
docker exec -u root ctf-toolbox bash -lc '/opt/ctf-toolbox/addons/install-common-addins.sh crypto'
```

Profile contents:
- `crypto`: `hashcat`, `john`, `hcxtools`, `steghide`, `outguess`, plus `pycryptodome`.
- `cloud`: `awscli`, `kubectl` (`kubernetes-client`), plus `ScoutSuite` and `trufflehog`.
- `forensics`: `sleuthkit`, `testdisk`, `foremost`, `binwalk`, `exiftool`, `tshark`, `yara`, plus `volatility3`.
- `pentest`: `nmap`, `ffuf`, `sqlmap`.
- `heavy`: `sagemath`, best-effort `radare2`, plus `angr`.
- `all`: installs default non-pentest profiles (`crypto`, `cloud`, `forensics`, `heavy`).

Optional proprietary tools are not bundled. Mount them under `/opt/licensed` on the VM and invoke manually.
