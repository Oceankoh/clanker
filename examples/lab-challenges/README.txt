clanker lab challenges — exercise the heavier capabilities (VPN reachability,
the gdb MCP / pwn tooling) that the trivial smoke-challenges don't.

Unlike examples/smoke-challenges (local-only, solvable in <1 min), these need
setup and are slower. Use them to validate VPN and MCP integration end-to-end.

Expected flags live in EXPECTED.tsv (kept at this root so they're never
uploaded to the VM). Operator-only files (the web server, the binary source)
also live at this root, NOT inside the per-challenge folders, so the agent only
sees what a real player would.

────────────────────────────────────────────────────────────────────────────
web-local — VPN REQUIRED
────────────────────────────────────────────────────────────────────────────
A web service the agent can only reach *over the managed VPN* (it runs on YOUR
laptop, on your LAN, which the VM routes to via WireGuard). This validates the
whole VPN path: deploy with VPN on, bring the tunnel up, agent reaches the LAN.

  1. Deploy the challenge with VPN ENABLED (do NOT pass --no-vpn / don't tick
     "--no-vpn" in the UI):
        scripts/ctfvm start --dir examples/lab-challenges/web-local --agent claude-code
  2. Bring the tunnel up locally (needs sudo):  ./scripts/ctfvm vpn --run-id <id> up
  3. Start the service on this laptop (binds all interfaces on :8000):
        python3 examples/lab-challenges/web-local-serve.py
     It prints the exact URL to hand the agent if you want it deterministic.

The agent finds the host from /workspace/vpn/*.json and GETs /flag.

────────────────────────────────────────────────────────────────────────────
pwn-overflow — gdb MCP / pwn tooling
────────────────────────────────────────────────────────────────────────────
A classic ret2win stack overflow (x86-64, no PIE, no stack canary). The agent
must find the saved-return offset and redirect execution to win() — the natural
workflow for the bundled gdb MCP + pwntools.

  scripts/ctfvm start --dir examples/lab-challenges/pwn-overflow --agent codex

The challenge folder ships the compiled binary `vuln` (so the agent reverses it,
like a real pwn). Source + build recipe are at this root (.pwn-overflow-src/),
operator-only. Rebuild with: examples/lab-challenges/.pwn-overflow-src/build.sh
(uses a linux/amd64 docker image so it matches the VM).
