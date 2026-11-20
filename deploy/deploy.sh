#!/bin/sh
# Deploy this code to manifold and restart the hosted gallery. Run from the project folder:
#     sh deploy/deploy.sh
set -eu
HOST=manifold
APP=/srv/photo-sync/app

echo "==> Copying code"
rsync -rt --delete --chmod=D755,F644 \
    --exclude=.git/ --exclude=.venv/ --exclude=__pycache__/ --exclude=err.out --exclude='/~' \
    ./ "$HOST:$APP/"

echo "==> Installing dependencies, checking config, restarting"
ssh "$HOST" "set -e
    cd $APP
    [ -x .venv/bin/python ] || python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
    .venv/bin/python -m compileall -q photosync
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1
    if systemctl is-enabled -q photo-sync 2>/dev/null; then
        sudo -n systemctl restart photo-sync
    else
        sudo -n systemctl enable --now photo-sync
    fi
    sudo -n systemctl reload caddy
    sleep 3
    systemctl is-active photo-sync"
echo "==> Live at https://frantierphotos.duckdns.org"
