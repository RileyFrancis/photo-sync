#!/bin/bash
# One-time admin setup for hosting the photo backup on manifold. Run once:
#     sudo bash ~/photo-sync-deploy/setup.sh
# Safe to run again; every step checks whether it's already done.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Run with sudo."; exit 1; fi
ADMIN=server                      # the everyday login account; deploys code and restarts the app
HERE="$(cd "$(dirname "$0")" && pwd)"
step() { printf '\n==> %s\n' "$1"; }

step "1/7 Use the whole drive (Ubuntu's installer only allocated 100 GB of it)"
FREE=$(vgs --noheadings --units g -o vg_free ubuntu-vg | tr -dc '0-9.')
if [ "${FREE%.*}" -gt 1 ]; then
    lvextend --resizefs -l +100%FREE /dev/ubuntu-vg/ubuntu-lv
else
    echo "Already done."
fi
df -h /

step "2/7 Install Caddy (web server with automatic HTTPS) from its official repository"
if ! command -v caddy >/dev/null; then
    apt-get update
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
else
    echo "Already installed: $(caddy version)"
fi

step "3/7 Install Python tools for the app"
apt-get install -y python3-venv python3-pip

step "4/7 Create the app's own account and folders"
# The app runs as 'photosync', which can't log in and can only write to /srv/photo-sync/data.
id photosync >/dev/null 2>&1 || useradd --system --home-dir /srv/photo-sync --shell /usr/sbin/nologin photosync
install -d -o "$ADMIN" -g "$ADMIN" -m 755 /srv/photo-sync /srv/photo-sync/app /srv/photo-sync/app/deploy
install -d -o photosync -g photosync -m 750 /srv/photo-sync/data
install -o "$ADMIN" -g "$ADMIN" -m 644 "$HERE/Caddyfile" /srv/photo-sync/app/deploy/Caddyfile
ls -ld /srv/photo-sync /srv/photo-sync/app /srv/photo-sync/data

step "5/7 Point Caddy at the app's config and install the app's service"
if ! grep -qx 'import /srv/photo-sync/app/deploy/Caddyfile' /etc/caddy/Caddyfile 2>/dev/null; then
    [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
    echo 'import /srv/photo-sync/app/deploy/Caddyfile' > /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
# The service file lives in /etc (root-owned), so changing what runs as the app still needs your password.
install -o root -g root -m 644 "$HERE/photo-sync.service" /etc/systemd/system/photo-sync.service
systemctl daemon-reload

step "6/7 Let '$ADMIN' restart the app and reload Caddy without a password (nothing else)"
cat > /etc/sudoers.d/photo-sync <<SUDO
$ADMIN ALL=(root) NOPASSWD: /usr/bin/systemctl reload caddy, /usr/bin/systemctl restart photo-sync, /usr/bin/systemctl stop photo-sync, /usr/bin/systemctl enable --now photo-sync
SUDO
chmod 440 /etc/sudoers.d/photo-sync
visudo -cf /etc/sudoers.d/photo-sync

step "7/7 Open ports 80 and 443 in the firewall (if the firewall is on)"
if ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp
    ufw allow 443/tcp
else
    echo "Firewall (ufw) is off; nothing to open."
fi

systemctl enable caddy >/dev/null 2>&1
systemctl reload caddy 2>/dev/null || systemctl restart caddy
printf '\nDone. Caddy is running and will get the HTTPS certificate once ports 80/443 are forwarded on the router.\n'
