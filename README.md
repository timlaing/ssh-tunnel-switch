# SSH Tunnel Switch

Rotate SSH SOCKS5 tunnels through a REST API or CLI. The built-in HTTP proxy forwards HTTP requests and HTTPS CONNECT traffic through the active tunnel, using SOCKS5 for remote DNS resolution.

The server runs independently and requires Python 3.12+ and OpenSSH. No OpenCode, Node.js, Bun or plugin is required.

## Install and run

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ssh-tunnel-switch
.venv/bin/tunnel-switch init-config --output config.yaml
```

`init-config` is available from version 0.1.1. It creates a mode-0600 example and refuses to overwrite existing files. For 0.1.0, download the [example configuration](https://raw.githubusercontent.com/timlaing/ssh-tunnel-switch/service-v0.1.0/examples/config.yaml) and protect it with `chmod 600 config.yaml`.

Edit the SSH host aliases in the YAML. Verify host keys and key/agent authentication using your normal SSH client before starting the service. SSH accounts must allow TCP forwarding. Existing `~/.ssh/config` settings apply when YAML does not override them.

```sh
.venv/bin/tunnel-switch validate --config config.yaml
.venv/bin/tunnel-switch serve --config config.yaml
```

Startup generates and saves an API token if needed and prints it to the console. Keep the configuration and logs private. By default the SOCKS5 listener is `127.0.0.1:1080`, the HTTP proxy is `127.0.0.1:8118`, and the REST API is `127.0.0.1:8765`.

## Use the proxy

```sh
curl --proxy http://127.0.0.1:8118 https://example.com/
.venv/bin/tunnel-switch status --config config.yaml
.venv/bin/tunnel-switch rotate --config config.yaml
```

Proxy-aware applications can use `HTTP_PROXY` and `HTTPS_PROXY` set to `http://127.0.0.1:8118`; set `NO_PROXY=localhost,127.0.0.1,::1` for direct local control calls. SOCKS-aware clients can use `socks5h://127.0.0.1:1080` directly. HTTPS remains encrypted end to end. Forwarding fails when SOCKS is unavailable and never falls back to a direct destination connection.

The bearer token protects the REST API only. HTTP and SOCKS proxy listeners are unauthenticated: retain loopback defaults or restrict access with a firewall. Protect remote REST access with TLS. See the [security policy](SECURITY.md).

## Guides

- [Configuration, restart state and REST API](docs/configuration.md)
- [Linux systemd deployment](docs/deployment.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Compatibility and tested platforms](docs/compatibility.md)
- [Development, review and releases](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE).
