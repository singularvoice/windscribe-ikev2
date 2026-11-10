"""Low level network helpers used by the daemon (run as root).

Sockets created here carry FWMARK so that they bypass both the tunnel routing
table and the kill switch: the daemon must be able to resolve server names and
measure latency even when the firewall blocks everything else.
"""

import ipaddress
import json
import os
import queue
import random
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.request

from .common import FWMARK

SO_MARK = getattr(socket, "SO_MARK", 36)


def run(cmd, check=True, input=None, timeout=60):
    """Run a command, return CompletedProcess. Raises RuntimeError on failure if check."""
    p = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}): {(p.stderr or p.stdout).strip()}")
    return p


def _marked_socket(family, type_, proto=0):
    s = socket.socket(family, type_, proto)
    try:
        s.setsockopt(socket.SOL_SOCKET, SO_MARK, FWMARK)
    except PermissionError:
        pass  # not root (mock / tests); still works without the bypass
    return s


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def upstream_dns_servers():
    """DNS servers of the physical links (from systemd-resolved), with public fallbacks."""
    servers = []
    for path in ("/run/systemd/resolve/resolv.conf", "/etc/resolv.conf"):
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] == "nameserver":
                        ip = parts[1].split("%")[0]
                        if is_ip(ip) and not ip.startswith(("127.", "fe80")) and ip not in servers:
                            servers.append(ip)
        except OSError:
            pass
        if servers:
            break
    for fb in ("1.1.1.1", "9.9.9.9"):
        if fb not in servers:
            servers.append(fb)
    return servers


def _encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna")
        if not 0 < len(raw) < 64:
            raise ValueError(f"invalid hostname {name!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _skip_name(buf, off):
    while True:
        ln = buf[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0 == 0xC0:
            return off + 2
        off += 1 + ln


def dns_query_a(name, server, timeout=3.0):
    """Minimal DNS A query over UDP (marked socket). Returns list of IPv4 strings."""
    qid = random.randint(0, 0xFFFF)
    pkt = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + _encode_name(name) + struct.pack(">HH", 1, 1)
    fam = socket.AF_INET6 if ":" in server else socket.AF_INET
    s = _marked_socket(fam, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(pkt, (server, 53))
        deadline = time.monotonic() + timeout
        while True:
            s.settimeout(max(0.1, deadline - time.monotonic()))
            data, _ = s.recvfrom(4096)
            if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == qid:
                break
    finally:
        s.close()
    _, flags, qd, an, _, _ = struct.unpack(">HHHHHH", data[:12])
    if flags & 0x000F:
        raise OSError(f"DNS error rcode={flags & 0xF} for {name}")
    off = 12
    for _ in range(qd):
        off = _skip_name(data, off) + 4
    ips = []
    for _ in range(an):
        off = _skip_name(data, off)
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
        off += 10
        if rtype == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[off:off + 4]))
        off += rdlen
    return ips


DNS_CACHE = os.environ.get("WSIKE_DNS_CACHE", "/var/lib/wsike/dns-cache.json")


def _cache_load():
    try:
        with open(DNS_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _cache_store(hostname, ips):
    try:
        cache = _cache_load()
        cache[hostname] = ips
        os.makedirs(os.path.dirname(DNS_CACHE), exist_ok=True)
        with open(DNS_CACHE + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(DNS_CACHE + ".tmp", DNS_CACHE)
    except OSError:
        pass


def resolve(hostname, timeout=6.0, bypass=True):
    """Resolve hostname -> IPv4 list.

    bypass=True (tunnel not up): query every upstream DNS server with marked
    sockets (passing the kill switch) and the system resolver in parallel and
    take the first answer.
    bypass=False (tunnel up): only use the system resolver, i.e. the VPN's DNS
    through the tunnel, so no lookup leaves outside of it.
    Falls back to the last known addresses when nothing answers.
    """
    if is_ip(hostname):
        return [hostname]
    result = queue.Queue()

    def via_server(server):
        for attempt in range(2):
            try:
                ips = dns_query_a(hostname, server, timeout=2.5)
                result.put(("ok", ips) if ips else ("err", f"{server}: no A records"))
                return
            except (OSError, ValueError, struct.error, IndexError) as e:
                err = f"{server}: {e}"
        result.put(("err", err))

    def via_system():
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_DGRAM)
            ips = list(dict.fromkeys(i[4][0] for i in infos))
            result.put(("ok", ips) if ips else ("err", "system: no addresses"))
        except OSError as e:
            result.put(("err", f"system: {e}"))

    workers = [threading.Thread(target=via_server, args=(srv,), daemon=True)
               for srv in (upstream_dns_servers() if bypass else [])]
    workers.append(threading.Thread(target=via_system, daemon=True))
    for w in workers:
        w.start()
    errors = []
    deadline = time.monotonic() + timeout
    while len(errors) < len(workers):
        try:
            kind, val = result.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            errors.append("timed out")
            break
        if kind == "ok":
            ips = [ip for ip in val if not ip.startswith(("127.", "0."))]
            if ips:
                _cache_store(hostname, ips)
                return ips
        errors.append(val)
        if time.monotonic() >= deadline:
            break
    cached = _cache_load().get(hostname)
    if cached:
        return cached
    raise RuntimeError(f"could not resolve {hostname} ({'; '.join(errors[:3])})")


def _checksum(data):
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def ping(ip, timeout=1.5):
    """ICMP echo round trip in ms (marked, bypasses tunnel), or None."""
    try:
        if os.geteuid() == 0:
            s = _marked_socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    except OSError:
        return None
    ident = random.randint(0, 0xFFFF)
    payload = b"wsike-ping" + os.urandom(6)
    hdr = struct.pack("!BBHHH", 8, 0, 0, ident, 1)
    pkt = struct.pack("!BBHHH", 8, 0, _checksum(hdr + payload), ident, 1) + payload
    try:
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.sendto(pkt, (ip, 0))
        deadline = t0 + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            s.settimeout(remaining)
            data, addr = s.recvfrom(2048)
            if addr[0] != ip:
                continue
            if s.type == socket.SOCK_RAW:
                data = data[(data[0] & 0x0F) * 4:]
            if len(data) >= 8 and data[0] == 0 and data.endswith(payload):
                return round((time.monotonic() - t0) * 1000)
    except OSError:
        return None
    finally:
        s.close()


def public_ip(timeout=5):
    """Public IPv4 as seen by the internet (unmarked: goes through tunnel when up)."""
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wsike"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read(64).decode().strip()
                if is_ip(ip):
                    return ip
        except Exception:
            continue
    return None


def default_route():
    """(gateway, device) of the main-table IPv4 default route, or (None, None)."""
    p = run(["ip", "-4", "route", "show", "table", "main", "default"], check=False)
    m = re.search(r"default(?: via (\S+))? dev (\S+)", p.stdout)
    return (m.group(1), m.group(2)) if m else (None, None)


def iface_ipv4(dev):
    p = run(["ip", "-4", "-o", "addr", "show", "dev", dev], check=False)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", p.stdout)
    return m.group(1) if m else None


def iface_exists(dev):
    return os.path.exists(f"/sys/class/net/{dev}")


def iface_bytes(dev):
    try:
        with open(f"/sys/class/net/{dev}/statistics/rx_bytes") as f:
            rx = int(f.read())
        with open(f"/sys/class/net/{dev}/statistics/tx_bytes") as f:
            tx = int(f.read())
        return rx, tx
    except OSError:
        return 0, 0


def normalize_cidr(value):
    """Validate a split-tunnel entry: IP, CIDR or hostname. Returns canonical string."""
    value = value.strip()
    if not value:
        raise ValueError("empty entry")
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        pass
    if re.fullmatch(r"(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}\.?", value):
        return value.rstrip(".").lower()
    raise ValueError(f"not an IP, subnet or hostname: {value!r}")
