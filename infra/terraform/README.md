# Base Infrastructure

These Terraform stacks provision the static network layer for the HTTP control-plane architecture:

- a dedicated VPC or subnet
- ingress rules for the control-plane port
- optional SSH ingress for manual break-glass access
- stable tags/identifiers that `./scripts/ctfvm start` can reuse for dynamically created VMs

Recommended workflow:

1. Apply the base stack for the provider you want.
2. Export the resulting network values into the repo `.env`.
3. Keep using `./scripts/ctfvm start` for per-run VM lifecycle.

The default control-plane port in these stacks is `443`. If you previously applied an older stack that only opened `8777`, re-apply Terraform so ingress matches the current `CTFVM_CONTROL_PORT`.

This keeps Terraform focused on long-lived shared resources while the CLI continues handling short-lived worker instances.
