#!/bin/bash
# Quick installer for chrony-dashboard on a Debian/Ubuntu/Raspberry Pi OS host.
# Run as root (e.g. `sudo bash install.sh`).
set -euo pipefail

USER_NAME="${CHRONY_DASHBOARD_USER:-chrony-dashboard}"
INSTALL_DIR="${CHRONY_DASHBOARD_DIR:-/opt/chrony-dashboard}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

echo "==> Installing dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends python3-flask chrony

echo "==> Creating service account $USER_NAME"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi

echo "==> Installing app to $INSTALL_DIR"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$INSTALL_DIR"
install -o "$USER_NAME" -g "$USER_NAME" -m 0755 "$SRC_DIR/app.py" "$INSTALL_DIR/app.py"

echo "==> Granting passwordless sudo for chronyc read commands"
cat > /etc/sudoers.d/chrony-dashboard <<EOF
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/chronyc clients, /usr/bin/chronyc serverstats
EOF
chmod 440 /etc/sudoers.d/chrony-dashboard

echo "==> Installing systemd unit"
sed "s|^User=.*|User=$USER_NAME|; s|^Group=.*|Group=$USER_NAME|; s|^WorkingDirectory=.*|WorkingDirectory=$INSTALL_DIR|; s|^ExecStart=.*|ExecStart=/usr/bin/python3 $INSTALL_DIR/app.py|" \
  "$SRC_DIR/chrony-dashboard.service" > /etc/systemd/system/chrony-dashboard.service

systemctl daemon-reload
systemctl enable --now chrony-dashboard.service

sleep 2
echo
echo "==> Service status"
systemctl --no-pager status chrony-dashboard.service | head -10 || true

echo
PORT="${CHRONY_DASHBOARD_PORT:-8080}"
IP=$(hostname -I | awk '{print $1}')
echo "Dashboard URL: http://${IP}:${PORT}/"
