#!/usr/bin/env python3
"""Tunnel-side DNS forwarder for `ctfvm vpn`.

Runs ON THE OPERATOR'S LAPTOP, bound to the WireGuard tunnel IP (the local
gateway address), and forwards every DNS query to the laptop's OWN system
resolvers. The VM/agent points its resolver at this address, so it resolves
exactly what the laptop can — internal/LAN names, a loopback proxy (dnscrypt,
a corporate client), and public names alike — without DHCP (which can't cross a
routed WireGuard tunnel) or knowing the venue's DNS server IP up front.

    sudo python3 scripts/vpn_dns_forwarder.py --bind 10.88.7.1 [--upstream 1.1.1.1] [--pidfile P]

Binding :53 needs root; `ctfvm vpn up` already runs privileged, so it starts
this for you when CTFVM_VPN_DNS is enabled. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import subprocess
import sys
import threading

FALLBACK = ["1.1.1.1", "8.8.8.8"]


def detect_upstreams(exclude: str = "") -> list[str]:
    """The laptop's current system resolvers (macOS scutil, else resolv.conf),
    minus our own bind address (to avoid a forwarding loop). Loopback resolvers
    are kept — they're valid here, since we run on the laptop."""
    out: list[str] = []
    try:  # macOS: the default resolver's nameservers
        txt = subprocess.run(["scutil", "--dns"], capture_output=True, text=True, timeout=5).stdout
        out = _parse_scutil(txt)
    except Exception:
        out = []
    if not out:
        try:
            out = _parse_resolv_conf(open("/etc/resolv.conf").read())
        except Exception:
            out = []
    out = [ip for ip in out if ip != exclude]
    return out or list(FALLBACK)


def _parse_scutil(text: str) -> list[str]:
    """Nameservers from the first ('resolver #1') block of `scutil --dns`."""
    block = text.split("resolver #2")[0]
    ips = re.findall(r"nameserver\[\d+\]\s*:\s*([0-9.]+)", block)
    seen, out = set(), []
    for ip in ips:
        if _is_ipv4(ip) and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _parse_resolv_conf(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("nameserver"):
            parts = line.split()
            if len(parts) >= 2 and _is_ipv4(parts[1]):
                out.append(parts[1])
    return out


def _is_ipv4(ip: str) -> bool:
    try:
        socket.inet_aton(ip)
        return ip.count(".") == 3
    except OSError:
        return False


def _split_hostport(up: str) -> tuple[str, int]:
    """`1.2.3.4` -> ('1.2.3.4', 53); `1.2.3.4:5353` -> ('1.2.3.4', 5353)."""
    if up.count(":") == 1:
        host, _, port = up.partition(":")
        return host, int(port)
    return up, 53


def _forward(query: bytes, upstreams: list[str], tcp: bool = False) -> bytes | None:
    for up in upstreams:
        host, port = _split_hostport(up)
        try:
            if tcp:
                with socket.create_connection((host, port), timeout=4) as s:
                    s.sendall(len(query).to_bytes(2, "big") + query)
                    n = int.from_bytes(_recv_exactly(s, 2), "big")
                    return _recv_exactly(s, n)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(4)
                s.sendto(query, (host, port))
                return s.recv(65535)
        except Exception:
            continue
    return None


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("short read")
        buf += chunk
    return buf


def _serve_udp(bind: str, port: int, upstreams: list[str]) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    while True:
        try:
            data, addr = srv.recvfrom(65535)
        except OSError:
            break
        threading.Thread(target=_handle_udp, args=(srv, data, addr, upstreams), daemon=True).start()


def _handle_udp(srv, data, addr, upstreams):
    resp = _forward(data, upstreams, tcp=False)
    if resp:
        try:
            srv.sendto(resp, addr)
        except OSError:
            pass


def _serve_tcp(bind: str, port: int, upstreams: list[str]) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(16)
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=_handle_tcp, args=(conn, upstreams), daemon=True).start()


def _handle_tcp(conn, upstreams):
    try:
        conn.settimeout(5)
        n = int.from_bytes(_recv_exactly(conn, 2), "big")
        query = _recv_exactly(conn, n)
        resp = _forward(query, upstreams, tcp=True)
        if resp:
            conn.sendall(len(resp).to_bytes(2, "big") + resp)
    except Exception:
        pass
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", required=True, help="tunnel IP to listen on (the local gateway address)")
    ap.add_argument("--port", type=int, default=53)
    ap.add_argument("--upstream", action="append", default=[], help="upstream resolver(s); default = the laptop's own")
    ap.add_argument("--pidfile", default="")
    args = ap.parse_args()

    upstreams = args.upstream or detect_upstreams(exclude=args.bind)
    if args.pidfile:
        try:
            open(args.pidfile, "w").write(str(os.getpid()))
        except OSError:
            pass
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"[vpn-dns] forwarding {args.bind}:{args.port} -> {', '.join(upstreams)}", flush=True)
    t = threading.Thread(target=_serve_tcp, args=(args.bind, args.port, upstreams), daemon=True)
    t.start()
    try:
        _serve_udp(args.bind, args.port, upstreams)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
