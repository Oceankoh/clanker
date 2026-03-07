---
name: webhook-site-callbacks
description: Create and use webhook.site endpoints for callback-driven security testing and CTF exploitation. Use when users need to verify outbound connectivity, capture blind SSRF/RCE callbacks, inspect request headers/body, or gather callback evidence for proof-of-execution.
---

# Webhook.site Callbacks

Use this workflow from supervisor sessions when callback evidence is needed and direct output is unavailable.

## Preconditions
- Obtain a callback URL from `https://webhook.site` (for example `https://webhook.site/<uuid>`).
- Export it before running tests:

```bash
export WEBHOOK_URL="https://webhook.site/<uuid>"
```

## Validate Endpoint

Send a known probe first to confirm endpoint reachability:

```bash
curl -i -X POST "${WEBHOOK_URL}" \
  -H "Content-Type: application/json" \
  -d '{"probe":"ctfvm","source":"supervisor","ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'"}'
```

## Build Callback Payloads

Construct concrete payload URLs and include a run marker:

```bash
RUN_MARKER="$(date -u +%Y%m%dT%H%M%SZ)-$RANDOM"
echo "${WEBHOOK_URL}/cb?marker=${RUN_MARKER}&vector=ssrf"
echo "${WEBHOOK_URL}/cb?marker=${RUN_MARKER}&vector=rce"
```

Inject these URLs into candidate vectors (SSRF fields, command arguments, template expressions, deserialization gadgets).

## Correlate Results

When callbacks arrive, correlate by marker and record:
- request time (UTC)
- method/path/query
- key headers (`User-Agent`, `X-Forwarded-For`, auth context)
- body snippets or command output evidence

## Report Back

Return a concise summary:
- validated vectors
- failed vectors
- strongest exploit chain supported by callback evidence
- follow-up tests to increase confidence

## Safety Rules
- Never send real credentials, tokens, or private data to callback endpoints.
- Keep callback payloads minimal and synthetic.
- Redact sensitive values before adding findings to shared logs.

## Failure Handling
- If callbacks do not appear, verify probe delivery first with `curl`.
- If probe works but exploit callback does not, treat vector as unconfirmed and move to the next hypothesis.
