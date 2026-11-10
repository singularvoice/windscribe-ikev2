#!/usr/bin/env bash
# Install wsike system-wide. Run with: sudo ./install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo $0" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
UPDATE=0
[[ "${1:-}" == "--update" ]] && UPDATE=1   # only refresh program files + restart
PREFIX=/opt/wsike
TARGET_USER="${SUDO_USER:-}"

if [[ $UPDATE -eq 0 ]]; then
echo "==> Installing dependencies"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  charon-systemd strongswan-swanctl libcharon-extauth-plugins libcharon-extra-plugins \
  libstrongswan-standard-plugins nftables iproute2 ca-certificates curl \
  python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
fi

echo "==> Copying files to $PREFIX"
rm -rf "$PREFIX"
install -d "$PREFIX"
cp -r "$SRC/wsike" "$SRC/bin" "$PREFIX/"
find "$PREFIX" -name '__pycache__' -prune -exec rm -rf {} +
chmod 755 "$PREFIX"/bin/*
ln -sf "$PREFIX/bin/wsike" /usr/local/bin/wsike
ln -sf "$PREFIX/bin/wsikectl" /usr/local/bin/wsikectl

echo "==> Configuring strongSwan"
install -m 644 "$SRC/packaging/strongswan-wsike.conf" /etc/strongswan.d/wsike.conf
install -d /etc/swanctl/x509ca
# Windscribe's IKEv2 servers use Let's Encrypt certificates.
for c in ISRG_Root_X1 ISRG_Root_X2; do
  [[ -f /etc/ssl/certs/$c.pem ]] && install -m 644 "/etc/ssl/certs/$c.pem" "/etc/swanctl/x509ca/wsike-$c.pem"
done
# Intermediates, in case a server does not send its chain (best effort).
for i in r10 r11 r12 r13 r14 e5 e6 e7 e8 e9; do
  [[ -s "/etc/swanctl/x509ca/wsike-le-$i.pem" ]] && continue
  if curl -fsS --max-time 10 -o "/tmp/wsike-$i.pem" "https://letsencrypt.org/certs/2024/$i.pem"; then
    install -m 644 "/tmp/wsike-$i.pem" "/etc/swanctl/x509ca/wsike-le-$i.pem"
  fi
  rm -f "/tmp/wsike-$i.pem"
done
rm -f /etc/wsike/swanctl.conf   # old location (blocked by AppArmor)
systemctl enable strongswan.service >/dev/null 2>&1 || true
systemctl restart strongswan.service

if [[ -d /etc/NetworkManager/conf.d ]]; then
  install -m 644 "$SRC/packaging/nm-wsike.conf" /etc/NetworkManager/conf.d/90-wsike.conf
  nmcli general reload conf >/dev/null 2>&1 || true
fi
install -d -m 700 /var/lib/wsike

echo "==> Setting up access for ${TARGET_USER:-root}"
getent group wsike >/dev/null || groupadd --system wsike
install -d -m 700 /etc/wsike
if [[ -n "$TARGET_USER" ]]; then
  usermod -aG wsike "$TARGET_USER"
  uid="$(id -u "$TARGET_USER")"
  python3 - "$uid" <<'PY'
import json, os, sys
p = "/etc/wsike/settings.json"
try:
    s = json.load(open(p))
except Exception:
    s = {}
uids = set(s.get("allowed_uids", [])) | {int(sys.argv[1])}
s["allowed_uids"] = sorted(uids)
json.dump(s, open(p, "w"), indent=2)
os.chmod(p, 0o644)
PY
fi

echo "==> Installing service and desktop entry"
install -m 644 "$SRC/packaging/wsike-daemon.service" /etc/systemd/system/wsike-daemon.service
install -m 644 "$SRC/packaging/wsike.desktop" /usr/share/applications/wsike.desktop
install -D -m 644 "$SRC/packaging/wsike.svg" /usr/share/icons/hicolor/scalable/apps/wsike.svg
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
systemctl daemon-reload
systemctl enable wsike-daemon.service
systemctl restart wsike-daemon.service

echo
echo "Done. Start 'wsike VPN' from your app menu (or run: wsike)."
echo "Then: Preferences → Account → enter the IKEv2 credentials from windscribe.com/getconfig/ikev2,"
echo "and add the server hostname(s) shown by the same generator with the + button."
