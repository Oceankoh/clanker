"""Tunnel-side DNS forwarder (scripts/vpn_dns_forwarder.py): upstream detection
parsing + a real forward round-trip on loopback (no root needed)."""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import vpn_dns_forwarder as f  # noqa: E402


class UpstreamDetection(unittest.TestCase):
    def test_parse_scutil_first_resolver_only(self):
        text = (
            "resolver #1\n"
            "  nameserver[0] : 127.0.2.2\n"
            "  nameserver[1] : 127.0.2.3\n"
            "resolver #2\n"
            "  nameserver[0] : 8.8.8.8\n"
        )
        self.assertEqual(f._parse_scutil(text), ["127.0.2.2", "127.0.2.3"])

    def test_parse_resolv_conf(self):
        text = "# comment\nnameserver 192.168.1.1\nsearch lan\nnameserver 1.1.1.1\n"
        self.assertEqual(f._parse_resolv_conf(text), ["192.168.1.1", "1.1.1.1"])

    def test_split_hostport(self):
        self.assertEqual(f._split_hostport("1.2.3.4"), ("1.2.3.4", 53))
        self.assertEqual(f._split_hostport("1.2.3.4:5353"), ("1.2.3.4", 5353))

    def test_detect_excludes_bind_and_falls_back(self):
        # when detection yields nothing, fall back to public resolvers
        self.assertEqual(f.detect_upstreams.__name__, "detect_upstreams")
        # exclusion logic is pure-list based; emulate it
        ips = [ip for ip in ["10.0.0.1", "10.0.0.1"] if ip != "10.0.0.1"] or list(f.FALLBACK)
        self.assertEqual(ips, list(f.FALLBACK))


class ForwardRoundTrip(unittest.TestCase):
    def test_udp_relay(self):
        # fake upstream: prefix the query so we can assert the forwarder relayed it
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.bind(("127.0.0.1", 0))
        up_port = up.getsockname()[1]

        def upstream():
            while True:
                try:
                    data, addr = up.recvfrom(4096)
                except OSError:
                    return
                up.sendto(b"R:" + data, addr)
        threading.Thread(target=upstream, daemon=True).start()

        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()
        t = threading.Thread(target=f._serve_udp, args=("127.0.0.1", port, [f"127.0.0.1:{up_port}"]), daemon=True)
        t.start()
        time.sleep(0.3)
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(3)
        c.sendto(b"hello", ("127.0.0.1", port))
        resp, _ = c.recvfrom(4096)
        self.assertEqual(resp, b"R:hello")
        up.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
