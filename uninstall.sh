#!/usr/bin/env bash
# Remove wsike. Run with: sudo ./uninstall.sh   (add --purge to also delete /etc/wsike)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Please run as root: sudo $0" >&2; exit 1; }

systemctl disable --now wsike-daemon.service 2>/dev/null || true
nft delete table inet wsike 2>/dev/null || true
for p in 5740 5745 5749 5750 5751 5752; do while ip -4 rule del priority $p 2>/dev/null; do :; done; done
ip -4 route flush table 5753 2>/dev/null || true
ip link del wsike0 2>/dev/null || true
rm -f /etc/systemd/system/wsike-daemon.service /usr/share/applications/wsike.desktop \
      /usr/share/icons/hicolor/scalable/apps/wsike.svg /usr/local/bin/wsike /usr/local/bin/wsikectl \
      /etc/strongswan.d/wsike.conf /etc/swanctl/x509ca/wsike-*.pem \
      /etc/swanctl/conf.d/wsike.conf /etc/NetworkManager/conf.d/90-wsike.conf
rm -rf /opt/wsike /run/wsike /var/lib/wsike
systemctl daemon-reload
systemctl restart strongswan.service 2>/dev/null || true
if [[ "${1:-}" == "--purge" ]]; then rm -rf /etc/wsike; groupdel wsike 2>/dev/null || true; fi
echo "wsike removed."
