# Changelog

## Unreleased

- Add timestamps to every service and HTTP access log line and write them to a rotating, mode-0600 log file (`logging` YAML section with `file` and `level`).
- Show a live terminal status display when `serve` runs interactively, while warnings and errors still reach stderr; non-interactive runs keep plain timestamped console lines for the journal. Fixes #6.
- Harden the log path: reject non-regular files such as directories and FIFOs, refuse group- or world-writable log directories, and open the log without following symlinks.

## 1.0.0 — 2026-10-15

- Resolve full-main Sonar findings: document rotation error responses, simplify proxy handlers and conditionals, remove redundant exceptions, and make test assertions independent.

- Keep active CONNECT uploads/responses alive while either direction is active.
- Remember the last successful SSH server across restarts; add `state_file` and `server_name`, CLI startup overrides, and standalone configuration discovery.
- Add `init-config` with a packaged mode-0600 example and no-overwrite behavior.
- Add disposable real OpenSSH tests, linting, CodeQL, contribution forms, labels and maintenance automation.
- Protect main and release tags, restrict publishing environments, and verify registry hashes/installations automatically after publication.
- Attach server-only installation files and distributions to future releases; document configuration, deployment, compatibility and troubleshooting.

## 0.1.0

- Initial standalone SSH tunnel rotation server, copied from the existing MIT-licensed project.
- SOCKS5 tunnels, HTTP forwarding, HTTPS CONNECT, bearer-authenticated REST API and CLI.
