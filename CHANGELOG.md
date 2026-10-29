# Changelog

## Unreleased

- Draw the status panel's server picker as a pop-up over the dashboard instead of a list appended beneath it: it starts below the panel's title border, replaces the rows it covers while the rows it does not reach keep showing the tunnel state, and its window is sized to the display, so opening it never extends past the display's last line. When the configured servers no longer fit the pop-up, the list scrolls inside it — a window follows the highlight a row at a time and the title counts what it hides (`Select server (3 more above, 5 more below)`). A display too short to spare any rows still keeps the highlighted one, so Enter always has something to confirm, and the picker's key hints, shortened to a single line, travel inside the pop-up box.

## 1.4.1 — 2026-10-25

- Add Ctrl+S as a second way to open the status panel's server picker, for terminals where the Option key types a character instead of sending an Escape sequence — macOS Terminal.app and iTerm2 send `ß` for Option+S until "Use Option as Meta key" is enabled, and Alt+S can never work there. Software flow control is now switched off while the panel is up, so Ctrl+S reaches the key map instead of pausing the output; the saved terminal settings restore it on exit. A terminal that refuses the change, or a build of `termios` without those flags, is reported in the log and keeps every other key working.

## 1.4.0 — 2026-10-24

- Let the status panel's rotate key (Ctrl+N) bypass the rotation cooldown: an interactive rotation is a deliberate request from the operator, so it is admitted whenever no other connection change is running. The API, Alt+R and Ctrl+E keep enforcing the cooldown as before.
- Add a server picker to the status panel: Alt+S lists the configured servers with the current one marked and the highlight starting on it, ↑/↓ or j/k move the highlight, Enter connects to the highlighted server alone (no failover, no cooldown) and Esc cancels. The picker owns the keyboard while open, so other keys do nothing until it closes.
- Add a request counter to the status panel: the `HTTP requests` row shows how many requests the HTTP proxy carried over the current SSH connection (`current`) and how many the connection before it carried (`last`). A forwarded request and a CONNECT each count once, a request the tunnel never carried counts nowhere, and the current total starts again whenever the tunnel connects to another server.
- Report the same totals on `GET /status` as `requests.current` and `requests.last`. A tunnel that exits on its own reports its own total as the last value until the next connection replaces it.

## 1.3.0 — 2026-10-19

- Apply configuration changes without restarting the service or dropping the tunnel: `serve` watches its YAML file, validates each change and swaps it in while the tunnel keeps running; `POST /reload` and `SIGHUP` (systemd's `ExecReload`) apply the file immediately. A reload that arrives while a rotation is in progress is refused with 429 and picked up again by the watcher once the rotation finishes.
- Adopt each change according to what it touches: servers, cooldown, timeouts, logging and the HTTP proxy upstream apply live; a changed SOCKS bind (`proxy.ip`/`proxy.port`) takes effect at the next connection change with a warning rather than rebinding a live tunnel; a changed API or HTTP proxy listener (`api.ip`/`api.port`, `http_proxy.*`) is reported as needing a restart.
- Add interactive keys to the status panel footer: Ctrl+R reloads the configuration, Ctrl+E connects, Ctrl+D disconnects, Alt+R restarts the current server and Ctrl+N rotates to the next; Ctrl+C remains the quit key.
- Report the running build's version everywhere it matters: `tunnel-switch --version`, the `version` field in `GET /status`, the status panel title and the `serve` startup banner.

## 1.2.0 — 2026-10-18

- Fail over to the next configured server within a single rotation when a server refuses SSH or never becomes ready, instead of ending the rotation and making the caller wait out the cooldown. Each server is tried at most once and the rotation stops at the first one that connects; if every server fails the request still returns 503 with the last failure in `last_error`, and the log names each attempt.
- Report an SSH connection failure by its exit code even when ssh dies as the startup deadline passes, rather than as a generic startup timeout.

## 1.1.1 — 2026-10-17

- Show the SSH server name beside the connection ID in the `serve` status panel, rendered as `<id> (<server name>)`.
- Publish the connection ID, start time and server name as one snapshot so the live status display cannot combine fields from different tunnel lifecycles.

## 1.1.0 — 2026-10-17

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
