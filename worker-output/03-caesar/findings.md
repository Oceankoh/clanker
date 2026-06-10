# CTF Run Findings

Started: 2026-06-07
Workspace: `/home/ctf/run/03-caesar/challenge`

## Summary
- Challenge states `cipher.txt` is ROT13-encoded.
- Ciphertext in `challenge/cipher.txt`: `synt{e0g13_vf_abg_pelcgb}`.
- Decoded locally with ROT13; no remote service or VPN interaction was needed.
- Decoded output saved to `/home/ctf/run/03-caesar/artifacts/rot13_decode.txt`.
- Flag: `flag{r0t13_is_not_crypto}`

## Reproduction
```bash
cd /home/ctf/run/03-caesar/challenge
tr 'A-Za-z' 'N-ZA-Mn-za-m' < cipher.txt > /home/ctf/run/03-caesar/artifacts/rot13_decode.txt
cat /home/ctf/run/03-caesar/artifacts/rot13_decode.txt
```

Expected output:

```text
flag{r0t13_is_not_crypto}
```
