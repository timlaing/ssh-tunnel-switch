# Security policy

Report vulnerabilities privately using [GitHub private vulnerability reporting](https://github.com/timlaing/ssh-tunnel-switch/security/advisories/new). Do not post sensitive details in public issues. If reporting is unavailable, request a private reporting channel without disclosing the vulnerability. No response-time guarantee is offered. Fixes target the latest release and current development branch.

## Deployment boundaries

- The bearer token protects REST status and rotation endpoints. HTTP and SOCKS
  proxy listeners have no authentication. Use loopback or firewall access limited
  to intended clients; do not expose an unrestricted proxy to the Internet.
- Use TLS in front of a remotely accessed REST API. HTTPS CONNECT transports
  destination TLS unchanged; the service does not decrypt it.
- The YAML and client configuration contain the REST token. Keep both private.
  The CLI deliberately prints the token at every startup, including to systemd
  journal logs. Restrict log access and redact diagnostics before sharing.
- Protect the dedicated SSH key and verify host keys. The service uses batch
  authentication and strict host-key checking. It runs as one account and reads
  that account's SSH configuration. Configuration-write access can change hosts
  and exposed listeners, so grant it only to trusted administrators.
- Token rotation: stop the service, replace `api.token` with a fresh secret (or
  set it to null to generate one on the next startup), start the service, update
  every client using that token. Existing tokens are loaded
  at startup; editing the file alone does not revoke the running token.
- Run one API worker and one service instance per SOCKS socket. Coordination is
  within one process, not across multiple instances.
- Forwarding fails when SOCKS is unavailable. Tunnel rotation does not establish
  that a provider quota was reset and does not replace provider account controls.

Dependency audits and updates complement review; passing audits are not a claim
that the code has no vulnerabilities. See the [deployment guide](docs/deployment.md)
for service-account and filesystem settings.
