# SSH Tunnel Switch

Rotate between SSH SOCKS5 tunnels using a REST API or CLI. A built-in HTTP proxy forwards HTTP requests and HTTPS CONNECT traffic through the active tunnel, with remote DNS resolution through SOCKS5.

The server runs independently. It requires Python 3.12+ and OpenSSH; no OpenCode, Node.js, Bun, or plugin is needed.

## Install and run

Install from a checkout until the first PyPI release is available:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
cp examples/config.yaml config.yaml
```

After publication, install with `python -m pip install ssh-tunnel-switch`. The executable remains `tunnel-switch`, and Python imports use `tunnel_switch`.

Edit `config.yaml` with your SSH host aliases and the SOCKS, HTTP proxy and API bind IP/port. Only `host` is required for each SSH server. `name` defaults to `host`; `user`, `port` and `identity_file` are optional overrides. When omitted (or null), the SSH command leaves them unset, so the system SSH client inherits settings from `~/.ssh/config` for the account running the service. Without configured SSH values, OpenSSH uses its own defaults. Identity paths relative to the YAML file resolve from its directory; `~` expands. YAML is mandatory for every CLI command. Unknown fields, invalid IPs/ports, duplicate server names and missing identity files are rejected.

A host-only server list can use existing SSH config aliases:

```yaml
servers:
  - host: ssh-one
  - host: ssh-two
```

Normal `HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump` and other SSH config settings apply. An explicit YAML `user`, `port` or `identity_file` is passed as `-l`, `-p` or `-i`. Existing configurations with named servers and explicit values still work. Effective server names must remain unique, including names derived from host aliases.

Authenticate to each SSH server once using your normal SSH client and verify its host key. The service requires key/agent authentication (`BatchMode=yes`) and known hosts (`StrictHostKeyChecking=yes`). It does not prompt for passwords or accept unknown host keys. The SSH accounts must permit TCP forwarding.

The API token lives in `api.token` in the YAML file. On startup, `serve` generates and saves a random token if the field is missing, null or blank. Existing tokens are reused. In both cases it prints `API token: ...` to the console before starting the API. Generated-token writes are atomic, preserve YAML comments, and save the file with mode 0600. If saving fails, startup fails rather than using an unsaved token.

```sh
.venv/bin/tunnel-switch validate --config config.yaml
.venv/bin/tunnel-switch serve --config config.yaml
```

No token environment variable is used. `status` and `rotate` read the saved YAML token; they do not generate one. `serve` starts the first tunnel unless `start_on_launch: false`. Ctrl-C cleans up its SSH process. Each command supports `--help`; command/configuration/network failures print an error and CLI help with a nonzero exit code.

Run exactly one service process for a given SOCKS socket. The supplied CLI uses one Uvicorn worker, without reload. It manages only SSH processes it spawned. An existing process occupying the SOCKS address is rejected rather than terminated. The cooldown also applies after startup and failed rotations, so an immediate rotation within the default 30 seconds returns 429.

## Use the proxy with any client

Start the service, then configure an HTTP proxy-aware client:

```sh
curl --proxy http://127.0.0.1:8118 https://example.com/
```

Alternatively, set `HTTP_PROXY` and `HTTPS_PROXY` to `http://127.0.0.1:8118` for clients that support those variables. Keep local control requests direct using `NO_PROXY=localhost,127.0.0.1,::1` or `curl --noproxy '*'`. SOCKS5-aware clients can use `socks5h://127.0.0.1:1080` directly.

The HTTP proxy defaults to enabled on loopback port 8118. It streams HTTP/1.1 requests and supports HTTPS CONNECT without decrypting TLS. Forwarding fails when SOCKS is unavailable; it never falls back to a direct destination connection. Rotation closes existing tunneled connections. The API remains responsive during rotation. There is no automatic SSH restart loop.

The HTTP and SOCKS listeners are unauthenticated. Keep them on loopback or restrict access with a firewall. The bearer token protects only the REST API. Use a TLS reverse proxy for remote API access. See [security](SECURITY.md).

## REST API and CLI

All endpoints require `Authorization: Bearer <token>`. FastAPI interactive API docs are available at `/docs`.

| Endpoint | Result |
| --- | --- |
| `GET /status` | State, connection ID, start time, server, SOCKS and HTTP proxy addresses, and last error |
| `POST /rotate` | Stops current tunnel, starts next server, returns status after readiness |

Overlapping rotations and cooldown violations return **429**, with `Retry-After`; callers must trigger again later. They are not queued. Startup/SSH failures return **503** and leave the service disconnected; another trigger after the cooldown advances to the next server. A single-server list restarts the same server. Status remains responsive during rotation and returns `state: "rotating"`; connection ID/start time are null until the new tunnel is ready. After SSH exits, status reports disconnected and clears active connection fields. There is no automatic restart loop.

```sh
.venv/bin/tunnel-switch status --config config.yaml
.venv/bin/tunnel-switch rotate --config config.yaml
curl --noproxy '*' -H "Authorization: Bearer <token-from-YAML>" http://127.0.0.1:8765/status
curl --noproxy '*' -X POST -H "Authorization: Bearer <token-from-YAML>" http://127.0.0.1:8765/rotate
```

Example connected status:

```json
{
  "state": "connected",
  "connection_id": "9d7e9746-c4e2-4a43-8da5-674ab452cb91",
  "started_at": "2026-10-14T12:00:00+00:00",
  "server": "first",
  "proxy": {"ip": "127.0.0.1", "port": 1080},
  "http_proxy": {"enabled": true, "ip": "127.0.0.1", "port": 8118},
  "last_error": null
}
```

## Development and deployment

- [Contributing and releases](CONTRIBUTING.md)
- [Linux systemd deployment](docs/deployment.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE).
