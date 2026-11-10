"""nftables rules: kill switch (firewall), DNS leak and IPv6 leak protection.

Everything lives in a single `inet wsike` table that is replaced atomically, so
there is never a moment where the old rules are gone and the new ones are not
yet in place.
"""

import ipaddress

from .common import FWMARK, IFACE, NFT_TABLE
from .netutil import run

LAN_V4 = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "224.0.0.0/4",
          "255.255.255.255/32"]
LAN_V6 = ["fe80::/10", "fc00::/7", "ff00::/8"]
# Router solicitation/advertisement, neighbour solicitation/advertisement.
ND_TYPES = "{ nd-router-solicit, nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert }"


def _set(items):
    return "{ " + ", ".join(items) + " }"


def build_ruleset(*, killswitch, connected, allow_lan, server_ips=(), bypass_v4=()):
    """Return an nft script, or None when no rules are needed at all.

    killswitch: block every packet that does not go through the tunnel.
    connected:  tunnel is (being) used -> enable DNS + IPv6 leak protection.
    bypass_v4:  IPv4 networks allowed outside the tunnel (exclusive split tunneling).
    """
    if not killswitch and not connected:
        return None

    server_ips = sorted({ip for ip in server_ips if ip})
    bypass_v4 = sorted({str(ipaddress.ip_network(n, strict=False)) for n in bypass_v4})
    mark = f"0x{FWMARK:x}"

    out = [
        'oifname "lo" accept',
        f"meta mark {mark} accept comment \"IKE/ESP and daemon traffic\"",
    ]
    if server_ips:
        out.append(f"ip daddr {_set(server_ips)} udp dport {{ 500, 4500 }} accept")
        out.append(f"ip daddr {_set(server_ips)} meta l4proto esp accept")

    if connected:
        # DNS leak protection: plain DNS and DoT may only leave through the tunnel.
        out.append(f'oifname != "{IFACE}" udp dport {{ 53, 853 }} reject')
        out.append(f'oifname != "{IFACE}" tcp dport {{ 53, 853 }} reject with tcp reset')

    out.append(f'oifname "{IFACE}" accept')

    # IPv6 is not carried by the tunnel: block it to avoid IPv6 leaks.
    out.append(f"icmpv6 type {ND_TYPES} accept")
    out.append("udp sport 546 udp dport 547 accept comment \"DHCPv6\"")
    if allow_lan:
        out.append(f"ip6 daddr {_set(LAN_V6)} accept")
    out.append("meta nfproto ipv6 reject with icmpx admin-prohibited")

    if killswitch:
        out.append("udp sport 68 udp dport 67 accept comment \"DHCP\"")
        if allow_lan:
            out.append(f"ip daddr {_set(LAN_V4)} accept")
        if bypass_v4:
            out.append(f"ip daddr {_set(bypass_v4)} accept comment \"split tunnel\"")
        out.append("reject with icmpx admin-prohibited")

    chains = [
        "  chain output {\n    type filter hook output priority filter; policy accept;\n"
        + "".join(f"    {r}\n" for r in out) + "  }\n"
    ]

    if killswitch:
        inp = [
            'iifname "lo" accept',
            "ct state established,related accept",
            f'iifname "{IFACE}" accept',
            "udp sport 67 udp dport 68 accept",
            f"icmpv6 type {ND_TYPES} accept",
            "udp sport 547 udp dport 546 accept",
        ]
        if allow_lan:
            inp.append(f"ip saddr {_set(LAN_V4)} accept")
            inp.append(f"ip6 saddr {_set(LAN_V6)} accept")
        inp.append("drop")
        fwd = [
            f'oifname "{IFACE}" accept',
            f'iifname "{IFACE}" ct state established,related accept',
        ]
        # Containers/VMs on host bridges keep talking to each other and the LAN;
        # only their internet-bound traffic is forced into the tunnel.
        fwd.append(f"ip daddr {_set(LAN_V4)} accept")
        if allow_lan:
            fwd.append(f"ip6 daddr {_set(LAN_V6)} accept")
        if bypass_v4:
            fwd.append(f"ip daddr {_set(bypass_v4)} accept")
        fwd.append("reject with icmpx admin-prohibited")
        chains.append("  chain input {\n    type filter hook input priority filter; policy accept;\n"
                      + "".join(f"    {r}\n" for r in inp) + "  }\n")
        chains.append("  chain forward {\n    type filter hook forward priority filter; policy accept;\n"
                      + "".join(f"    {r}\n" for r in fwd) + "  }\n")

    return f"table inet {NFT_TABLE} {{\n" + "".join(chains) + "}\n"


def apply(script):
    """Atomically replace the wsike table with `script` (None = remove it)."""
    # Creating then deleting first makes the transaction valid whether or not
    # the table exists already.
    tx = f"table inet {NFT_TABLE}\ndelete table inet {NFT_TABLE}\n"
    if script:
        tx += script
    run(["nft", "-f", "-"], input=tx)


def check(script):
    """Syntax-check a ruleset without applying it."""
    run(["nft", "-c", "-f", "-"], input=script)


def remove():
    run(["nft", "delete", "table", "inet", NFT_TABLE], check=False)
