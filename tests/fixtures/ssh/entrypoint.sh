#!/bin/sh
set -eu
if [ ! -f /tmp/ssh_host_ed25519_key ]; then
    ssh-keygen -q -t ed25519 -N '' -f /tmp/ssh_host_ed25519_key
fi
printf '%s\n' "$TUNNEL_TEST_NAME" > /tmp/www/index.html
python3 -m http.server 8080 --bind 127.0.0.1 --directory /tmp/www >/dev/null 2>&1 &
exec /usr/sbin/sshd -D -e
