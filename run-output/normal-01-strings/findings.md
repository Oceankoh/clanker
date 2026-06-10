# CTF Run Findings

Started: 2026-06-07T14:46:52Z
Workspace note: `/workspace` was missing in the container, so it was created as a symlink to `/home/ctf/run`. The challenge is available at `/workspace/challenge`.

## Challenge

`/workspace/challenge/description.txt` says `data.bin` contains a hidden flag in `flag{...}` format buried in random noise.

## File Facts

- Target: `/workspace/challenge/data.bin`
- Type: ASCII text
- Size: 816 bytes
- SHA-256: `1a5712e5d9eceff4c0a32e9a8b4350323f61812a57499dd5cd62b8313c5a2958`

## Reproduction

Commands run locally:

```bash
strings /workspace/challenge/data.bin | grep -i 'flag'
grep -a -i -n -o 'flag{[^}]*}' /workspace/challenge/data.bin
```

Both commands recover the same flag. The second command reports it on line 32.

## Artifacts

- `/workspace/artifacts/strings_grep_flag.txt`
- `/workspace/artifacts/grep_flag_offsets.txt`

## Flag

`flag{str1ngs_4lways_w0rk}`
