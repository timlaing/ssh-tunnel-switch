# Deploying the Python service

See the [configuration guide](configuration.md) for YAML fields and API behavior.

The supplied [systemd unit](../deploy/systemd/tunnel-switch.service) targets
Linux with systemd, Python 3.12 or newer, and OpenSSH. macOS users can run the
foreground CLI described in the README; no launchd service is supplied.
These instructions install files on a deployment host, not on your workstation.

## Install

Use an approved source checkout at `/opt/tunnel-switch`, or download the verified installation files attached to releases from 1.0.0 onward. No GitHub authentication is needed for this public repository. Code and the virtual environment should be root-owned and not writable by the service account. On Debian/Ubuntu, install `openssh-client` and the `python3-venv` package matching a supported Python first.

For a release installation, download every attached asset into an empty directory, run `sha256sum -c SHA256SUMS`, then extract `ssh-tunnel-switch-install.tar.gz` into `/opt/tunnel-switch` as root. The archive contains examples, deployment files and guides; the wheel is attached separately. Version 0.1.0 has no installation bundle: use its source archive for those files.

```sh
sudo useradd --system --user-group --create-home \
  --home-dir /var/lib/tunnel-switch --shell /usr/sbin/nologin tunnel-switch
sudo install -d -m 0700 -o tunnel-switch -g tunnel-switch /etc/tunnel-switch
sudo install -d -m 0700 -o tunnel-switch -g tunnel-switch /var/lib/tunnel-switch/.ssh
sudo python3 -m venv /opt/tunnel-switch/.venv
sudo /opt/tunnel-switch/.venv/bin/python -m pip install 'ssh-tunnel-switch==1.4.0'
sudo install -m 0600 -o tunnel-switch -g tunnel-switch \
  /opt/tunnel-switch/examples/config.yaml /etc/tunnel-switch/config.yaml
sudoedit /etc/tunnel-switch/config.yaml
```

For a source installation, replace the pip argument with `/opt/tunnel-switch`. A verified downloaded wheel can also be installed by its absolute path.

Run `useradd` only when the account does not already exist. The configuration
directory is writable by the service because saving a newly generated token
uses an atomic file replacement. The unit permits writes only to that directory,
the service's home, and its private temporary directory. Root owns the executable
and unit. Configuration values can select SSH hosts, identities and bind sockets,
so write access to the YAML is operational control of the service.

## Configure SSH under the service account

Host-only YAML entries use `/var/lib/tunnel-switch/.ssh/config`, not your own
account's SSH configuration. Provision the necessary SSH config, known-host
entries and a dedicated authentication key for this account. Set `.ssh` to mode
0700 and config/private-key files to 0600 with owner `tunnel-switch`.

Example SSH config, with deployment-specific values substituted:

```sshconfig
Host ssh-one
    HostName ssh-one.example.org
    User tunnel
    IdentityFile /var/lib/tunnel-switch/.ssh/id_ed25519
    IdentitiesOnly yes
```

Verify each server's host-key fingerprint through a trusted channel before
adding it to this account's `known_hosts`. Test the effective configuration and
key authentication as the actual account:

```sh
sudo -u tunnel-switch ssh -G ssh-one
sudo -u tunnel-switch ssh -o BatchMode=yes -o StrictHostKeyChecking=yes ssh-one true
sudo -u tunnel-switch /opt/tunnel-switch/.venv/bin/tunnel-switch \
  validate --config /etc/tunnel-switch/config.yaml
```

Repeat for every host. A restricted SSH account may reject the `true` remote
command even when authentication works; use its administrator-approved check
instead. SSH diagnostic output can contain private hostnames and paths.

The unit does not inherit your desktop SSH agent. Encrypted keys require a
separately managed agent reachable by this service; its socket must also be
accessible inside the unit sandbox. A dedicated noninteractive key should have
narrow server-side permissions and carefully restricted local access. The SSH
server must allow TCP forwarding. `ProxyJump` works when its authentication
requirements are also satisfied under this account.

State defaults to `/var/lib/tunnel-switch/.config/ssh-tunnel-switch/state.json`
for this account, within the unit's writable home directory. For another location,
set `state_file` in YAML or override `ExecStart` with `--state-file`; the directory
must be writable by this account and permitted by `ReadWritePaths`. Use
`--server-name` only to deliberately override the remembered startup selection.

## Start and verify

Keep the default loopback listeners for a local deployment. Install the unit:

```sh
sudo install -m 0644 /opt/tunnel-switch/deploy/systemd/tunnel-switch.service \
  /etc/systemd/system/tunnel-switch.service
sudo systemd-analyze verify /etc/systemd/system/tunnel-switch.service
sudo systemctl daemon-reload
sudo systemctl enable --now tunnel-switch.service
sudo systemctl status tunnel-switch.service
sudo -u tunnel-switch /opt/tunnel-switch/.venv/bin/tunnel-switch \
  status --config /etc/tunnel-switch/config.yaml
```

The status command should report `connected`, a connection ID and a start time.
`systemctl` reporting `active` only confirms the Python process is running; it
does not confirm SSH connectivity. Configure clients after checking `/status`.

Startup prints the API token as required by the CLI, so under systemd it appears
in the journal. Timestamped service and access logs are also written to
`logging.file` (default `~/.config/ssh-tunnel-switch/tunnel-switch.log` for the
service account). Read either only through a trusted local administrator
session:

```sh
sudo journalctl -u tunnel-switch.service -n 50 --no-pager
```

Do not paste this output into issues or share it unredacted. See [SECURITY.md](../SECURITY.md).

## Remote clients and updates

For remote clients, bind the HTTP proxy to a reachable interface and
restrict both HTTP and SOCKS access using the host firewall. Keep SOCKS on
loopback when only the built-in bridge needs it. Protect a remotely accessed
REST API with TLS at a reverse proxy and configure clients to use HTTPS.
The built-in API serves HTTP; it does not terminate TLS. The CLI accesses the
configured local HTTP socket directly. Wildcard API bindings are mapped to
loopback by CLI status/rotate commands.

The unit hides `/home`, `/root` and `/run/user`; place SSH files in its own home
and adjust any `Include`, `IdentityFile` or `ProxyJump` dependencies accordingly.
Its filesystem restrictions follow [systemd's execution settings](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

Before upgrading, preserve a protected copy of the configuration and current
release bundle. Stop the service, extract the approved new release and install
its wheel into the existing virtual environment, validate YAML as the service
account, then start it and verify status. Restore the previous bundle and wheel
and configuration if validation fails.

Configuration changes no longer require a service restart: the service watches
its YAML and applies them without dropping the tunnel, and `systemctl reload
tunnel-switch` triggers the same reload immediately through the unit's
`ExecReload` SIGHUP. Servers, timeouts, the API token and logging changes take
effect at once; a changed `proxy` socket is adopted by the next rotation; the
API and HTTP proxy listen addresses are created at startup and still need a
restart, which the log and `config_reload.deferred` in `/status` name. Changing
HTTP proxy settings also requires updating clients.

Start the unit with `systemctl start` after an upgrade, or `systemctl reload
tunnel-switch` after a configuration-only change, and confirm `/status` shows
the expected server with no `config_reload.last_error`.

The unit restarts a failed Python process. An SSH subprocess exiting while the
API stays alive does not trigger a systemd restart; use `/status`, diagnose SSH,
and request rotation after the cooldown. `KillMode=mixed` lets the Python process
clean up its tunnel before systemd forcibly stops remaining child processes at
the shutdown deadline. Keep `TimeoutStopSec` longer than configured stop timeouts.
