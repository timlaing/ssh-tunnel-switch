# Compatibility

| Component | Requirement or verification |
| --- | --- |
| Python | 3.12+; CI tests 3.12, 3.13 and 3.14 on Linux and 3.13 on macOS. |
| OpenSSH | A system `ssh` client with dynamic SOCKS forwarding, known hosts and noninteractive key/agent authentication. |
| Real SSH integration | Linux CI uses disposable Docker OpenSSH servers to test forwarding, rotation, strict host-key rejection, server loss, recovery and cleanup. No personal SSH credentials are used. |
| HTTP proxy | HTTP/1.1 absolute-URL forwarding and HTTPS CONNECT. HTTP Upgrade is unsupported; CONNECT carries destination TLS unchanged. |
| Service deployment | Linux systemd example included. Foreground operation is tested on macOS; no launchd unit is supplied. Windows is not tested. |
| Python distributions | Wheel and sdist install in fresh environments. Examples and license are checked; the wheel ships the `init-config` template. |
| External clients | Any supported HTTP/SOCKS proxy client or bearer-authenticated REST client. No plugin dependency. |

Version 0.1.0 requires explicit YAML configuration and starts with the first SSH server. Version 1.0.0 adds configuration discovery, persistent restart selection, startup overrides, `init-config`, and the CONNECT stream inactivity fix. The `tunnel-switch` CLI, `tunnel_switch` imports and REST endpoint shapes are retained.

Local tests use fixtures and do not establish that a particular remote SSH account or client application is correctly configured. See the current [CI runs](https://github.com/timlaing/ssh-tunnel-switch/actions/workflows/ci.yml) and release notes for evidence tied to a revision.
