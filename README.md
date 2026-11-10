# wsike: IKEv2 client for Windscribe on Linux

A desktop VPN client in the style of the Windscribe app, built for **IKEv2**,
which the official Linux client doesn't offer. Developed for Ubuntu (GTK4 + libadwaita,
strongSwan, nftables, systemd-resolved).

Not affiliated with Windscribe.

## Features

- **One-click connect** with a Windscribe-style UI: big power button, ON/OFF header,
  location list with flags, favourites, search and latency.
- **Firewall (kill switch)**: all traffic that doesn't go through the tunnel is blocked.
  Modes:
  - *Manual*: the toggle stays where you put it.
  - *Automatic*: on while connected/reconnecting, off after you disconnect.
  - *Always on*: blocks even when disconnected and across reboots.
  In Automatic mode an *unexpected* drop keeps the firewall up until you disconnect
  or switch it off, so nothing leaks while you are away.
  Option: *Allow LAN traffic*.
- **No DNS leaks**: the VPN's DNS is set on `wsike0` with routing domain `~.` in
  systemd-resolved. While connected, plain DNS and DoT (53/853) to any other
  interface are rejected. The DNS server IPs are always routed into the tunnel.
- **No IPv6 leaks**: the tunnel is IPv4 only, so IPv6 is blocked while connected
  (and while the firewall is on), except LAN/neighbour discovery.
- **No IP leaks on drops**: the tunnel is route-based (XFRM interface `wsike0`).
  If the tunnel goes down, traffic is routed into a dead interface and, with the
  firewall on, rejected. Auto-reconnect brings the tunnel back.
- **Split tunneling** by IP, subnet or hostname:
  - *Exclusive*: listed destinations bypass the VPN.
  - *Inclusive*: only listed destinations use the VPN.
- Custom DNS, connect on boot, a debug log, and the `wsikectl` CLI.

## Install

```bash
sudo ./install.sh
```

The installer adds `charon-systemd`, `strongswan-swanctl`, the EAP plugins and the
GTK/libadwaita bindings. It also links the ISRG (Let's Encrypt) root certificates
into `/etc/swanctl/x509ca`. Windscribe servers send their own intermediate
certificates, so no downloads are needed. Finally it creates the `wsike` group,
gives the invoking user access, and enables `wsike-daemon.service`.

Options:

- `sudo ./install.sh --update` refreshes wsike's own files and restarts the daemon, without apt.
- `sudo ./install.sh --user NAME` grants access to another desktop user.

### Supported systems

- **Tested:** Ubuntu 26.10 (GNOME, NetworkManager, systemd-resolved). A clean
  install was also checked in a fresh `ubuntu:26.10` container.
- **Expected to work:** Ubuntu 24.04 or newer and Debian 13 or newer: any apt-based
  system with systemd, nftables, strongSwan 5.9 or newer and libadwaita 1.5 or newer.
  The installer checks these and stops with a clear message if something is missing.
- **Without systemd-resolved,** DNS is written to `/etc/resolv.conf` while connected
  and restored afterwards.
- **Conflicts:** the classic `strongswan-starter`/`ipsec` service and other IKE
  daemons use the same ports (UDP 500/4500). Running another VPN at the same time
  will also interfere with routing and DNS.

## Setup

1. Open <https://windscribe.com/getconfig/ikev2>, choose a location and generate the
   config. Note the **IKEv2 username, password and server hostname**. These are
   different from your Windscribe login.
2. Start **wsike VPN** from the app menu (or run `wsike`).
3. Go to ☰ → Preferences → Account and save the IKEv2 username and password.
4. Click **+**, paste the server hostname, and give it a name and country code.
   To add many servers at once, use ☰ → Import locations, one per line:
   `Name, hostname, cc`.
5. Click a location or the power button.

CLI equivalents:

```bash
wsikectl login
```

```bash
wsikectl connect <hostname> --wait
```

```bash
wsikectl set firewall_mode=always_on allow_lan=true
```

```bash
wsikectl logs
```

## How it works

```
 GTK app / wsikectl  (your user)
        │  JSON over /run/wsike/wsike.sock (SO_PEERCRED-checked)
        ▼
 wsike-daemon (root, systemd)
   ├─ swanctl / charon-systemd  →  IKEv2, EAP-MSCHAPv2, server cert = hostname (Let's Encrypt)
   ├─ ip link wsike0 type xfrm if_id 0x5753   (route-based IPsec)
   ├─ ip rule: fwmark 0x5753 → main, main w/o default, else → table 5753 (default dev wsike0)
   ├─ nft table inet wsike: kill switch + DNS/IPv6 leak rules (atomic replace)
   └─ resolvectl: DNS on wsike0, domain ~.
```

IKE packets (charon socket fwmark) and ESP packets (`set_mark_out`) carry mark
`0x5753`, so they always leave through the physical interface and are let through by
the firewall. Everything else goes through `wsike0`. This is the approach `wg-quick`
uses, and it keeps working when you switch between Wi-Fi and Ethernet (MOBIKE is
enabled too).

Files:
- `/etc/wsike/credentials.json`: IKEv2 credentials (root, mode 0600)
- `/etc/wsike/settings.json`: daemon settings
- `/etc/strongswan.d/wsike.conf`: charon-systemd settings (does not affect NetworkManager's charon-nm)
- `~/.config/wsike/servers.json`: your locations

## Development

Run without root, using a fake VPN backend:

```bash
WSIKE_SOCKET=$XDG_RUNTIME_DIR/wsike-mock.sock WSIKE_STATE_DIR=/tmp/wsike-state python3 bin/wsike-daemon --mock
```

```bash
WSIKE_SOCKET=$XDG_RUNTIME_DIR/wsike-mock.sock python3 bin/wsike
```

Tests (nft rulesets are syntax-checked inside an unprivileged network namespace):

```bash
python3 -m unittest discover -s tests -v
```

## Limitations

- IPv4 only through the tunnel (IPv6 is blocked rather than tunnelled).
- Split tunneling works by destination (IP, subnet or hostname), not per application.
- No tray icon: GTK4 has no tray API. The VPN keeps running in the daemon when you close the window.
- Windscribe has no public IKEv2 server list API, so you add servers from the config generator.
- Uninstall with `sudo ./uninstall.sh`. Add `--purge` to also remove credentials and settings.
