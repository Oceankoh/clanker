clanker smoke-test challenges.

Each subfolder is one self-contained challenge (description.txt + ideas.txt +
the challenge file). Solvable in well under a minute with the toolbox, so the
provision -> solve -> teardown loop stays fast.

  01-strings  flag hidden in noise         -> strings/grep
  02-base64   base64-encoded secret        -> base64 -d
  03-caesar   ROT13 cipher                 -> tr
  04-hidden   flag in a DOTFILE (.flag)    -> verifies hidden files upload intact

Expected flags live in EXPECTED.tsv (kept at this root, NOT inside any
challenge subdir, so it is never uploaded to the VM — the agent can't cheat).

Run one challenge end-to-end against a real VM (provision -> solve -> destroy):

  scripts/smoke.sh --agent claude-code --dir .ctf-work/smoke-challenges/01-strings

Fan the whole folder out from the UI: "+ New run" -> tick "deploy folder".
