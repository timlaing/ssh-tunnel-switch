# Configuration reference

Use `tunnel-switch init-config --output config.yaml` to create the [example YAML](../examples/config.yaml). Edit the SSH host aliases before starting the service.

## Python service configuration

Edit `config.yaml` with your SSH host aliases and the SOCKS, HTTP proxy and API bind IP/port. Only `host` is required for each SSH server. `name` defaults to `host`; `user`, `port` and `identity_file` are optional overrides. When omitted (or null), the SSH command leaves them unset, so the system SSH client inherits settings from `~/.ssh/config` for the account running the service. Without configured SSH values, OpenSSH uses its own defaults. Identity paths relative to the YAML file resolve from its directory; `~` expands. YAML is required for service commands (the `init-config` command creates an example). Unknown fields, invalid IPs/ports, duplicate server names and missing identity files are rejected.

| YAML field                | Default                                                            | Purpose                                                                                           |
| ------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- |
| `servers`                 | Required nonempty list                                             | SSH aliases and optional per-server overrides                                                     |
| `proxy`                   | Required section                                                   | SSH SOCKS listener; `ip` defaults to `127.0.0.1`, `port` to `1080`                                |
| `http_proxy`              | Enabled at `127.0.0.1:8118`                                        | HTTP forwarding and HTTPS CONNECT listener; see the proxy section below                           |
| `api`                     | `127.0.0.1:8765`                                                   | REST bind address and optional `token`; missing tokens are generated on startup                   |
| `logging`                 | File `~/.config/ssh-tunnel-switch/tunnel-switch.log`, level `INFO` | Timestamped service and HTTP access logs; see the logging section below                           |
| `cooldown_seconds`        | `30`                                                               | Minimum time after a completed or failed rotation before the next attempt; zero disables cooldown |
| `startup_timeout_seconds` | `15`                                                               | SSH connection/readiness deadline, greater than zero and at most 120 seconds                      |
| `stop_timeout_seconds`    | `5`                                                                | Wait before forcibly killing an SSH process, greater than zero and at most 60 seconds             |
| `start_on_launch`         | `true`                                                             | Start the saved SSH server (or the first server without saved state) when the API starts          |
| `state_file`              | `~/.config/ssh-tunnel-switch/state.json`                           | Last successfully connected server; `null` disables persistence                                   |
| `server_name`             | `null`                                                             | Optional starting server name, overriding saved state                                             |

A host-only server list can use existing SSH config aliases:

```yaml
servers:
  - host: ssh-one
  - host: ssh-two
```

Normal `HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump` and other SSH config settings apply. An explicit YAML `user`, `port` or `identity_file` is passed as `-l`, `-p` or `-i`. Existing configurations with named servers and explicit values still work. Effective server names must remain unique, including names derived from host aliases.

Authenticate to each SSH server once using your normal SSH client and verify its host key. The service requires key/agent authentication (`BatchMode=yes`) and known hosts (`StrictHostKeyChecking=yes`). It does not prompt for passwords or accept unknown host keys. The SSH accounts must permit TCP forwarding.

The API token lives in `api.token` in the YAML file. On startup, `serve` generates and saves a random token if the field is missing, null or blank. Existing tokens are reused. In both cases it prints `API token: ...` to the console before starting the API. Generated-token writes are atomic, preserve YAML comments, and save the file with mode 0600. If saving fails, startup fails rather than using an unsaved token.

When `--config` is omitted, all CLI commands use
`~/.config/ssh-tunnel-switch/config.yaml` if it exists, otherwise
`./config.yaml` in the current working directory. An explicit `--config` takes
precedence. Invalid or unreadable files report an error without falling back.

```sh
.venv/bin/tunnel-switch validate --config config.yaml
.venv/bin/tunnel-switch serve --config config.yaml
```

No token environment variable is used. `status` and `rotate` read the saved YAML token; they do not generate one. `serve` resumes the saved SSH server unless `start_on_launch: false`; without saved state it starts the first server. Ctrl-C cleans up its SSH process. Each command supports `--help`; command/configuration/network failures print an error and CLI help with a nonzero exit code.

Run exactly one service process for a given SOCKS socket. The supplied CLI uses one Uvicorn worker; its configuration is reloaded live (see below), not by restarting the worker. It manages only SSH processes it spawned. An existing process occupying the SOCKS address is rejected rather than terminated. The cooldown also applies after startup and failed rotations, so an immediate rotation within the default 30 seconds returns 429.

## Reloading configuration

`serve` watches the YAML file it was started with and applies changes without
restarting, so the API stays up and the established SSH tunnel keeps running.
Polling runs every two seconds and ignores a rewrite whose content is
unchanged; `serve --no-watch` disables polling only, leaving the manual
triggers below available.

Reloads are triggered by the file watch, by `POST /reload`, or by `SIGHUP`
(the supplied unit ships `ExecReload=/bin/kill -HUP $MAINPID`, so
`systemctl reload tunnel-switch` works). A reload is refused while a rotation
is in progress, so a rotation never sees a half-changed server list.

The file is validated in full before anything is applied. If it is invalid —
for example an unknown key or a missing identity file — the running
configuration is kept, the failure is logged, and it is reported as
`config_reload.last_error` in `GET /status`. A bad edit can therefore never
stop the service or drop the tunnel. CLI startup overrides (`--state-file`,
`--server-name`) outlive reloads for the life of the process.

Applied immediately:

- `servers` (used by the next rotation), `cooldown_seconds`,
  `startup_timeout_seconds`, `stop_timeout_seconds`
- `api.token`, so a rotated token takes effect on the next request
- `state_file`, `logging.file` and `logging.level` (the log handlers are
  rebuilt)
- every `http_proxy` limit that is read per connection: the timeouts,
  `header_limit_bytes` and `max_connections`

Deferred, because they are baked into an already-bound socket, and named in
`config_reload.deferred` as well as in the log:

- `proxy.ip` and `proxy.port`: the running `ssh -D` keeps its socket, and the
  new bind is adopted by the next connection change — `POST /rotate`, or the
  Alt+R/Ctrl+N/Ctrl+E keys below
- `api.ip` and `api.port`, and `http_proxy.enabled`, `http_proxy.ip` and
  `http_proxy.port`: these listeners are only created at startup, so they
  require a service restart. The HTTP proxy limits above still apply live.

## Logging and the status display

Every service and HTTP access log line carries a UTC ISO 8601 timestamp, for
example `2026-10-16T09:15:00Z INFO uvicorn.access: 127.0.0.1:58993 - "GET
/status HTTP/1.1" 200 OK`. Logs answer when a rotation happened and whether it
succeeded, including SSH server names and connection failures, so you can tell a
rotate apart from ordinary health polling.

Timestamped lines are written to `logging.file` (rotating at 1 MiB with three
backups, created mode 0600) so they survive after the terminal is closed. YAML
log paths are relative to the YAML file and `~` expands; `file: null` disables
file logging. `logging.level` filters the service and Uvicorn loggers; valid
levels are `DEBUG`, `INFO`, `WARNING`, `ERROR` and `CRITICAL`.

On an interactive terminal, `serve` shows a live status panel (state, server,
connection ID shown as `<id> (<server name>)`, elapsed time, proxy addresses
and the last error) instead of console log lines:
routine records reach only the file, while warnings and errors still print to
stderr, so failures such as a port bind error remain visible even with
`file: null`, where those warnings and errors are then the only recorded
output. When stdout is not a terminal — for example under systemd — plain
timestamped lines also go to the journal at the configured level and the status
panel is not shown.

When both stdout and stdin are terminals on a POSIX system, the panel also
shows the last reload, the last key action and a key footer. Keys are read in
cbreak mode, so Ctrl-C is untouched and still stops the service:

| Key    | Action                                                                           |
| ------ | -------------------------------------------------------------------------------- |
| Ctrl+E | connect to the last server (a hint when already connected)                       |
| Ctrl+D | stop the tunnel; this also clears the cooldown, so connecting again is immediate |
| Alt+R  | reconnect to the current server                                                  |
| Ctrl+N | rotate to the next server                                                        |
| Ctrl+R | reload the configuration, as `POST /reload` does                                 |
| Ctrl+C | stop the service (unchanged: SIGINT, normal uvicorn shutdown)                    |

Each connection change goes through the same cooldown and busy checks as the
API, and a rejection (for example an active cooldown) is reported in the
panel's last-action row rather than lost.

The log path must differ from the YAML and explicitly configured identity files
and must be a regular file (a directory or FIFO is rejected). At write time the
log directory must not be group-writable or world-writable — a sticky directory
such as `/tmp` is exempt — and the final log-file component is opened without
following symlinks. This stops the log file itself being replaced by a symlink
that would redirect appends onto a protected file; it does not protect against
an attacker who can replace an ancestor directory in the resolved path.

## Restart state and startup selection

The service saves the last successfully connected server to
`~/.config/ssh-tunnel-switch/state.json` by default, using the home of the
account running it. On restart it reconnects to that server, then subsequent
rotations advance through the configured list. Servers are identified by name
and host rather than list position, so reordering the YAML does not lose the
selection. Each new SSH session still has a fresh connection ID and start time.

Configure a different file in YAML or override it for a single run:

```yaml
state_file: ~/.config/ssh-tunnel-switch/state.json
server_name: null
```

```sh
.venv/bin/tunnel-switch serve --config config.yaml --state-file /path/to/state.json
.venv/bin/tunnel-switch serve --config config.yaml --server-name two
```

`--server-name` selects a server by its configured `name`. It takes precedence
over YAML `server_name` and saved state for the first connection, including the
first manual rotation when `start_on_launch` is false. A successful connection
updates the file even when selected explicitly. Later rotations advance normally.
Unknown server names are rejected. YAML state paths are relative to the YAML
file; relative CLI state paths use the current working directory. `~` expands
for the service account. Use a separate state file for each service instance.

Missing files, or saved servers removed/renamed in YAML, start at the first server.
Malformed, unsupported-version or unreadable state files cause startup to fail
with an error; move the file aside or explicitly select `--server-name` to
replace invalid contents on the next successful connection. Set `state_file: null`
in YAML to opt out. `--state-file` overrides that opt-out.

State is written only after SSH readiness succeeds, using atomic replacement and
mode 0600. Parent directories are created as needed; the service account needs
write access there. Failed SSH attempts leave the previous saved selection intact,
and a rotation that falls through to a later server saves the one that actually
connected. A persistence failure fails that rotation and closes its new SSH process rather
than reporting an unremembered success. Shutdown preserves the file. It contains
only a format version, server name and host, with no API token or private key.
The state path must differ from the YAML and explicitly configured identity files.

## REST API and CLI

The `/status`, `/rotate` and `/reload` endpoints require `Authorization: Bearer <token>`. FastAPI interactive API docs are available at `/docs`.

| Endpoint       | Result                                                                                                                                                                                                                     |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /status`  | State, connection ID, start time, server, SOCKS and HTTP proxy addresses, last error, and the `config_reload` block (watch state, config path, last reload, last error and deferred changes)                               |
| `POST /rotate` | Stops the current tunnel and connects via the next server, falling through the rest of the list when a server refuses SSH, then returns status after readiness                                                             |
| `POST /reload` | Re-reads the YAML and applies every change that needs no rebind; returns `applied`, `unchanged`, `refused`, `error`, `deferred` and the resulting server count. **404** when the service was started without a config file |

Overlapping rotations and cooldown violations return **429**, with `Retry-After`; callers must trigger again later. They are not queued. A rotation begins with the next server and, when that server exits SSH without becoming ready, moves straight on to the following server: each server is tried at most once, with no cooldown in between, and the rotation stops at the first one that accepts the connection. Only when every server has failed does the request return **503**, leaving the service disconnected, recording the last failure in `last_error` and naming every attempt in the log. A rotation therefore waits at most `startup_timeout_seconds` for each server it has to fall through. The cooldown covers the rotation as a whole, so it applies after a failed rotation too. A single-server list restarts the same server. Status remains responsive during rotation and returns `state: "rotating"`; connection ID/start time are null until the new tunnel is ready. After SSH exits, status reports disconnected and clears active connection fields. There is no automatic restart loop.

`POST /reload` returns **429** while a rotation is in progress, and **404** when
the service was started without a configuration file; a YAML that fails
validation is reported as `error` in a **200** response and as
`config_reload.last_error` in `/status`, with the previous configuration kept.
A rotation (`POST /rotate`) also adopts a `proxy` change that an earlier
reload had to defer, since it creates a fresh `ssh` process — but only once the
rotation is admitted, so a request rejected by the cooldown or a concurrent
rotation leaves the still-running tunnel on its original bind. The file watcher
retries the same way: a reload that finds a rotation in progress is refused and
picked up again on the next poll.

```sh
.venv/bin/tunnel-switch status --config config.yaml
.venv/bin/tunnel-switch rotate --config config.yaml
curl --noproxy '*' -H "Authorization: Bearer <token-from-YAML>" http://127.0.0.1:8765/status
curl --noproxy '*' -X POST -H "Authorization: Bearer <token-from-YAML>" http://127.0.0.1:8765/rotate
curl --noproxy '*' -X POST -H "Authorization: Bearer <token-from-YAML>" http://127.0.0.1:8765/reload
```

Example connected status:

```json
{
  "state": "connected",
  "connection_id": "9d7e9746-c4e2-4a43-8da5-674ab452cb91",
  "started_at": "2026-10-14T12:00:00+00:00",
  "server": "first",
  "proxy": { "ip": "127.0.0.1", "port": 1080 },
  "http_proxy": { "enabled": true, "ip": "127.0.0.1", "port": 8118 },
  "last_error": null,
  "config_reload": {
    "watching": true,
    "path": "/etc/tunnel-switch/config.yaml",
    "last_reload": "14:22:31 applied (3 servers)",
    "last_error": null,
    "deferred": []
  }
}
```
