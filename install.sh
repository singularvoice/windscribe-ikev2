#!/usr/bin/env bash
# Install wsike system-wide.
#
#   sudo ./install.sh                 full install (packages + files + services)
#   sudo ./install.sh --update        refresh program files and restart only
#   sudo ./install.sh --user NAME     grant NAME access (default: the sudo user)
set -euo pipefail

die()  { echo "error: $*" >&2; exit 1; }
warn() { echo "warning: $*" >&2; }

[[ $EUID -eq 0 ]] || die "please run as root: sudo $0 $*"

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/wsike
UPDATE=0
TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" && -n "${PKEXEC_UID:-}" ]]; then
  TARGET_USER="$(id -nu "$PKEXEC_UID" 2>/dev/null || true)"
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --update) UPDATE=1 ;;
    --user) shift; TARGET_USER="${1:-}"; [[ -n "$TARGET_USER" ]] || die "--user needs a name" ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
  shift
done
[[ "$TARGET_USER" == "root" ]] && TARGET_USER=""
if [[ -n "$TARGET_USER" ]] && ! id "$TARGET_USER" >/dev/null 2>&1; then
  die "user '$TARGET_USER' does not exist"
fi

# ------------------------------------------------------------------ checks
command -v apt-get >/dev/null || die "this installer needs an apt based system (Ubuntu/Debian)"
[[ -d /run/systemd/system ]] || die "systemd is required"
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  case "${ID:-}:${ID_LIKE:-}" in
    ubuntu:*|debian:*|*:*ubuntu*|*:*debian*) ;;
    *) warn "untested distribution '${PRETTY_NAME:-unknown}', continuing anyway" ;;
  esac
fi
for unit in strongswan-starter.service ipsec.service; do
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    warn "$unit is running; it uses the same IKE ports (500/4500) as wsike and will conflict"
  fi
done

if [[ $UPDATE -eq 0 ]]; then
  if nft list table inet wsike >/dev/null 2>&1 && [[ ! -e /sys/class/net/wsike0 ]]; then
    die "the wsike firewall is blocking traffic and the VPN is not connected, so packages cannot be
       downloaded. Connect the VPN or turn the firewall off (Preferences → Firewall mode → Manual),
       or use '--update' to only refresh wsike's own files."
  fi
  echo "==> Installing dependencies"
  apt-get -o Acquire::Retries=3 update -qq \
    || warn "apt-get update reported errors (mirror sync?); continuing with the existing package lists"
  DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=3 install -y --no-install-recommends \
    charon-systemd strongswan-swanctl libcharon-extauth-plugins libcharon-extra-plugins \
    libstrongswan-standard-plugins nftables iproute2 ca-certificates \
    python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
fi

# Verify what the program needs actually exists (also covers --update on an
# incomplete system).
for bin in swanctl nft ip; do command -v "$bin" >/dev/null || die "$bin not found (run without --update)"; done
ls /usr/lib/ipsec/plugins/libstrongswan-eap-mschapv2.so >/dev/null 2>&1 \
  || die "strongSwan EAP-MSCHAPv2 plugin missing (package libcharon-extauth-plugins)"
python3 - <<'PY' || die "GTK 4 / libadwaita >= 1.5 is required (Ubuntu 24.04 or newer)"
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
PY
modprobe xfrm_interface 2>/dev/null || [[ -d /sys/module/xfrm_interface ]] \
  || warn "kernel module xfrm_interface could not be loaded; the tunnel will not work"

# Stop the daemon first so it cannot overwrite settings we change below.
# (With the firewall in "Always on" mode the block stays in place meanwhile.)
systemctl stop wsike-daemon.service 2>/dev/null || true

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
install -d /etc/swanctl/x509ca /etc/swanctl/conf.d
# Windscribe servers present Let's Encrypt certificates and send their own
# intermediates, so trusting the ISRG roots is enough. Symlinks keep them in
# sync with the ca-certificates package (allowed by Ubuntu's AppArmor profile).
rm -f /etc/swanctl/x509ca/wsike-*.pem
found=0
for c in /etc/ssl/certs/ISRG_Root_*.pem; do
  [[ -e "$c" ]] || continue
  ln -sf "$c" "/etc/swanctl/x509ca/wsike-$(basename "$c")"
  found=1
done
[[ $found -eq 1 ]] || die "ISRG root certificates not found in /etc/ssl/certs (package ca-certificates)"
rm -f /etc/wsike/swanctl.conf   # pre-0.1.1 location (blocked by AppArmor)
systemctl enable strongswan.service >/dev/null 2>&1 || true
systemctl restart strongswan.service

if [[ -d /etc/NetworkManager/conf.d ]]; then
  install -m 644 "$SRC/packaging/nm-wsike.conf" /etc/NetworkManager/conf.d/90-wsike.conf
  nmcli general reload conf >/dev/null 2>&1 || true
fi

echo "==> Setting up access"
getent group wsike >/dev/null || groupadd --system wsike
install -d -m 700 /etc/wsike /var/lib/wsike
if [[ -n "$TARGET_USER" ]]; then
  echo "    granting access to $TARGET_USER"
  usermod -aG wsike "$TARGET_USER"
  python3 - "$(id -u "$TARGET_USER")" <<'PY'
import json, os, sys
p = "/etc/wsike/settings.json"
try:
    with open(p) as f:
        s = json.load(f)
except (OSError, ValueError):
    s = {}
s["allowed_uids"] = sorted(set(s.get("allowed_uids", [])) | {int(sys.argv[1])})
with open(p, "w") as f:
    json.dump(s, f, indent=2)
os.chmod(p, 0o644)
PY
else
  warn "no desktop user detected; run 'sudo ./install.sh --update --user <name>' or add users to the 'wsike' group"
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
