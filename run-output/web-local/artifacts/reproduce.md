# Reproduce / Resume Notes

Current status: blocked because the managed VPN is not present in this container.

Observed facts:

- `/workspace/vpn` is absent.
- `ip addr` shows only loopback and Docker `eth0`.
- `ip route` has no routed CTF VPN CIDR.
- `wg`, `wg-quick`, and `ctfvm` are not available.
- `127.0.0.1:8000` and `172.17.0.1:8000` do not serve `/flag`.

When the run is restarted with VPN enabled, use:

```bash
find /workspace/vpn -maxdepth 2 -type f -print -exec sed -n '1,220p' {} \;
ip addr show
ip route show table all
wg show
```

Then retrieve the flag from the peer/service host:

```bash
curl -i -m 5 http://<peer-ip>:8000/flag
```

If the peer IP does not answer, sweep only the routed VPN CIDR from the config:

```bash
for h in $(seq 1 254); do
  ip="10.x.y.$h"
  curl -sS -m 1 "http://${ip}:8000/flag" | sed "s/^/${ip} /" &
done
wait
```

Replace `10.x.y` with the actual CIDR prefix from `/workspace/vpn`.
