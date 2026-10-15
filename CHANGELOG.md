# Changelog

## 0.1.1 — Unreleased

- Keep active CONNECT uploads/responses alive while either direction is active.
- Remember the last successful SSH server across restarts; add `state_file` and `server_name`, CLI startup overrides, and standalone configuration discovery.
- Add `init-config` with a packaged mode-0600 example and no-overwrite behavior.
- Add disposable real OpenSSH tests, linting, CodeQL, contribution forms, labels and maintenance automation.
- Protect main and release tags, restrict publishing environments, and verify registry hashes/installations automatically after publication.
- Attach server-only installation files and distributions to future releases; document configuration, deployment, compatibility and troubleshooting.

## 0.1.0

- Initial standalone SSH tunnel rotation server, copied from the existing MIT-licensed project.
- SOCKS5 tunnels, HTTP forwarding, HTTPS CONNECT, bearer-authenticated REST API and CLI.
