# SSH Tunnel Switch

[![CI](https://github.com/timlaing/ssh-tunnel-switch/actions/workflows/ci.yml/badge.svg)](https://github.com/timlaing/ssh-tunnel-switch/actions/workflows/ci.yml)
[![CodeQL](https://github.com/timlaing/ssh-tunnel-switch/actions/workflows/codeql.yml/badge.svg)](https://github.com/timlaing/ssh-tunnel-switch/actions/workflows/codeql.yml)
[![PyPI](https://img.shields.io/pypi/v/ssh-tunnel-switch)](https://pypi.org/project/ssh-tunnel-switch/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Ftimlaing%2Fssh-tunnel-switch%2Fmain%2Fpyproject.toml)](https://pypi.org/project/ssh-tunnel-switch/)
[![License](https://img.shields.io/github/license/timlaing/ssh-tunnel-switch)](LICENSE)

[![Lines of Code](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=ncloc)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Code style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Duplicated Lines (%)](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=duplicated_lines_density)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=coverage)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)

[![Vulnerabilities](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=vulnerabilities)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Code Smells](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=code_smells)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Bugs](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=bugs)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)
[![Technical Debt](https://sonarcloud.io/api/project_badges/measure?project=timlaing_ssh-tunnel-switch&metric=sqale_index)](https://sonarcloud.io/summary/new_code?id=timlaing_ssh-tunnel-switch)

Rotate SSH SOCKS5 tunnels through a REST API or CLI. The built-in HTTP proxy forwards HTTP requests and HTTPS CONNECT traffic through the active tunnel, using SOCKS5 for remote DNS resolution.

The server runs independently and requires Python 3.12+ and OpenSSH. No OpenCode, Node.js, Bun or plugin is required.

## Install and run

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ssh-tunnel-switch
.venv/bin/tunnel-switch init-config --output config.yaml
```

`init-config` is available from version 1.0.0. It creates a mode-0600 example and refuses to overwrite existing files. For 0.1.0, download the [example configuration](https://raw.githubusercontent.com/timlaing/ssh-tunnel-switch/service-v0.1.0/examples/config.yaml) and protect it with `chmod 600 config.yaml`.

Edit the SSH host aliases in the YAML. Verify host keys and key/agent authentication using your normal SSH client before starting the service. SSH accounts must allow TCP forwarding. Existing `~/.ssh/config` settings apply when YAML does not override them.

```sh
.venv/bin/tunnel-switch validate --config config.yaml
.venv/bin/tunnel-switch serve --config config.yaml
```

Startup generates and saves an API token if needed and prints it to the console. Keep the configuration and logs private. On an interactive terminal `serve` shows a live status panel while timestamped service and access logs are written to the log file (`logging.file`); when run under systemd the timestamped lines also reach the journal. By default the SOCKS5 listener is `127.0.0.1:1080`, the HTTP proxy is `127.0.0.1:8118`, and the REST API is `127.0.0.1:8765`.

The service watches its YAML and applies configuration changes without restarting or dropping the tunnel (`systemctl reload` or `POST /reload` trigger it immediately). On an interactive terminal the status panel also responds to keys: Ctrl+R reloads the configuration, Ctrl+E connects, Ctrl+D disconnects, Alt+R restarts the current server, Ctrl+N rotates to the next, and Ctrl+C still stops the service.

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
