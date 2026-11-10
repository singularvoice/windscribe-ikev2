"""wsike privileged daemon.

Runs as root (systemd unit wsike-daemon.service). The unprivileged GUI talks to
it over a Unix socket using one JSON object per line.
"""

import argparse
import collections
import grp
import json
import os
import pwd
import signal
import socket
import struct
import sys
import threading
import time

from . import firewall, netutil
from .common import IFACE, SOCKET_PATH

STATE_DIR = os.environ.get("WSIKE_STATE_DIR", "/etc/wsike")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")
CREDS_FILE = os.path.join(STATE_DIR, "credentials.json")

DEFAULT_SETTINGS = {
    "firewall_mode": "automatic",   # manual | automatic | always_on
    "firewall_on": False,           # manual toggle state
    "allow_lan": True,
    "split_mode": "off",            # off | exclusive | inclusive
    "split_list": [],               # IPs, CIDRs, hostnames
    "custom_dns": [],               # empty = use DNS pushed by the server
    "auto_reconnect": True,
    "connect_on_boot": False,
    "last_server": None,            # {"hostname":..., "name":...}
    "allowed_uids": [],
}


def _write_json(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Firewall:
    def __init__(self, mock=False):
        self.mock = mock
        self.last = None

    def apply(self, script):
        if script == self.last:
            return
        if not self.mock:
            firewall.apply(script)
        self.last = script


class Daemon:
    def __init__(self, backend, fw, log_to_stderr=True):
        self.backend = backend
        self.fw = fw
        self.log_to_stderr = log_to_stderr
        self.logs = collections.deque(maxlen=2000)
        self.lock = threading.RLock()           # protects state fields
        self.op_lock = threading.Lock()         # serialises connect/disconnect
        self.settings = {**DEFAULT_SETTINGS, **_read_json(SETTINGS_FILE, {})}
        self.state = "disconnected"
        self.error = None
        self.server = None
        self.info = {}
        self.since = None
        self.public_ip = None
        self.want_connected = False
        # Automatic firewall mode: once connected, keep blocking after an
        # unexpected loss until the user explicitly disconnects.
        self.hold_firewall = False
        self.has_credentials = bool(self.credentials())
        self.stop_event = threading.Event()
        backend.log = self.log

    # ------------------------------------------------------------------ utils
    def log(self, msg):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        self.logs.append(line)
        if self.log_to_stderr:
            print(line, file=sys.stderr, flush=True)

    def save_settings(self):
        _write_json(SETTINGS_FILE, self.settings, 0o644)

    def credentials(self):
        return _read_json(CREDS_FILE, None)

    def set_state(self, state, error=None):
        with self.lock:
            if state != self.state:
                self.log(f"state: {self.state} -> {state}" + (f" ({error})" if error else ""))
            self.state = state
            self.error = error
            if state == "connected":
                self.hold_firewall = True
                if self.since is None:
                    self.since = time.time()
            if state in ("connecting", "disconnected", "error"):
                self.since = None
                self.public_ip = None
        self.refresh_firewall()

    def killswitch_active(self):
        s = self.settings
        if s["split_mode"] == "inclusive":
            return False      # "only these through the VPN" cannot block the rest
        if s["firewall_mode"] == "always_on" or s["firewall_on"]:
            return True
        return s["firewall_mode"] == "automatic" and (self.hold_firewall or self.state in (
            "connecting", "connected", "reconnecting"))

    def split_networks(self):
        nets = []
        for entry in self.settings.get("split_list", []):
            try:
                norm = netutil.normalize_cidr(entry)
            except ValueError:
                continue
            if "/" in norm:
                if ":" not in norm:
                    nets.append(norm)
                continue
            try:
                nets.extend(f"{ip}/32" for ip in netutil.resolve(norm))
            except RuntimeError as e:
                self.log(f"split tunnel: {e}")
        return nets

    def refresh_firewall(self):
        with self.lock:
            active = self.state in ("connecting", "connected", "reconnecting")
            bypass = self.info.get("bypass", []) if self.settings["split_mode"] == "exclusive" else []
            script = firewall.build_ruleset(
                killswitch=self.killswitch_active(),
                connected=active,
                allow_lan=self.settings["allow_lan"],
                server_ips=[self.info.get("server_ip")],
                bypass_v4=bypass,
            )
        try:
            self.fw.apply(script)
        except Exception as e:  # never let a firewall error crash the daemon
            self.log(f"firewall error: {e}")

    # -------------------------------------------------------------- operations
    def _connect_worker(self, server):
        with self.op_lock:
            if not self.want_connected:
                return
            creds = self.credentials()
            if not creds:
                self.want_connected = False
                self.set_state("error", "No IKEv2 credentials saved. Add them in Preferences → Account.")
                return
            with self.lock:
                self.server = server
                self.info = {}
            self.set_state("connecting")
            try:
                if self.backend.server_ip:      # switching servers
                    self.backend.disconnect()
                bypass = self.split_networks() if self.settings["split_mode"] != "off" else []
                with self.lock:
                    self.info["bypass"] = bypass
                info = self.backend.connect(
                    server["hostname"], creds["username"], creds["password"],
                    split_mode=self.settings["split_mode"], split_v4=bypass,
                    custom_dns=self.settings.get("custom_dns") or [])
            except Exception as e:
                cancelled = not self.want_connected     # user pressed disconnect
                self.log(f"connect {'cancelled' if cancelled else 'failed'}: {e}")
                try:
                    self.backend.disconnect()
                except Exception as e2:
                    self.log(f"cleanup failed: {e2}")
                self.want_connected = False
                with self.lock:
                    self.info = {}
                if cancelled:
                    self.set_state("disconnected")
                else:
                    self.set_state("error", str(e))
                return
            if not self.want_connected:      # user pressed disconnect meanwhile
                self.backend.disconnect()
                self.set_state("disconnected")
                return
            with self.lock:
                self.info.update(info)
            self.set_state("connected")
            self.settings["last_server"] = server
            self.save_settings()
        threading.Thread(target=self._fetch_public_ip, daemon=True).start()

    def _fetch_public_ip(self):
        ip = netutil.public_ip()
        with self.lock:
            self.public_ip = ip

    def connect(self, hostname, name=None):
        hostname = (hostname or "").strip()
        if not hostname:
            raise ValueError("hostname required")
        self.want_connected = True
        server = {"hostname": hostname, "name": name or hostname}
        threading.Thread(target=self._connect_worker, args=(server,), daemon=True).start()

    def disconnect(self):
        self.want_connected = False
        if self.state == "disconnected":
            return
        if self.state == "error":
            # nothing is up; just release the firewall hold (explicit user action)
            with self.lock:
                self.hold_firewall = False
            self.set_state("disconnected")
            return

        def worker():
            self.set_state("disconnecting")
            # makes a pending initiate/re-initiate return now instead of
            # waiting for its timeout while holding op_lock
            try:
                self.backend.abort()
            except Exception as e:
                self.log(f"abort: {e}")
            with self.op_lock:
                try:
                    self.backend.disconnect()
                except Exception as e:
                    self.log(f"disconnect error: {e}")
                with self.lock:
                    self.info = {}
                    self.hold_firewall = False
                if self.settings["firewall_mode"] == "automatic":
                    self.settings["firewall_on"] = False
                    self.save_settings()
                self.set_state("disconnected")
        threading.Thread(target=worker, daemon=True).start()

    def monitor(self):
        failures = 0
        while not self.stop_event.wait(3):
            if self.state not in ("connected", "reconnecting") or not self.want_connected:
                failures = 0
                continue
            if not self.op_lock.acquire(blocking=False):
                continue
            try:
                if self.backend.is_up():
                    if not self.backend.routes_ok():
                        self.backend.repair_routes(self.settings.get("custom_dns") or [])
                    if self.state == "reconnecting":
                        self.set_state("connected")
                    failures = 0
                    continue
                if not self.settings["auto_reconnect"]:
                    self.log("tunnel lost")
                    self.backend.disconnect()
                    self.want_connected = False
                    self.set_state("error", "Connection lost")
                    continue
                failures += 1
                self.set_state("reconnecting")
                if failures <= 2:          # let charon's DPD restart do its job first
                    continue
                self.log(f"tunnel down, re-initiating (attempt {failures - 2})")
                info = self.backend.reinitiate(self.settings.get("custom_dns") or [])
                if info:
                    with self.lock:
                        self.info.update(info)
                    self.set_state("connected")
                    failures = 0
            except Exception as e:
                self.log(f"monitor: {e}")
            finally:
                self.op_lock.release()
            if self.state == "reconnecting" and failures > 5:
                # charon lost the config (restart?) -> full reconnect
                self.log("full reconnect")
                failures = 0
                server = self.server
                if server and self.want_connected:
                    self._connect_worker(server)

    # ------------------------------------------------------------- requests
    def status(self):
        with self.lock:
            rx, tx = netutil.iface_bytes(IFACE) if self.state == "connected" else (0, 0)
            return {
                "state": self.state,
                "error": self.error,
                "server": self.server,
                "since": self.since,
                "server_ip": self.info.get("server_ip"),
                "vip": self.info.get("vip"),
                "dns": self.info.get("dns", []),
                "public_ip": self.public_ip,
                "firewall": self.killswitch_active(),
                "firewall_mode": self.settings["firewall_mode"],
                "rx": rx,
                "tx": tx,
                "has_credentials": self.has_credentials,
            }

    def handle(self, cmd, args):
        if cmd == "status":
            return self.status()
        if cmd == "connect":
            self.connect(args.get("hostname"), args.get("name"))
            return True
        if cmd == "disconnect":
            self.disconnect()
            return True
        if cmd == "get_settings":
            return {k: v for k, v in self.settings.items() if k != "allowed_uids"}
        if cmd == "set_settings":
            return self.update_settings(args)
        if cmd == "set_firewall":
            if self.settings["firewall_mode"] == "always_on":
                raise ValueError("Firewall is in Always On mode")
            self.settings["firewall_on"] = bool(args.get("on"))
            if not self.settings["firewall_on"]:
                self.hold_firewall = False
            self.save_settings()
            self.refresh_firewall()
            return self.killswitch_active()
        if cmd == "set_credentials":
            user, pw = args.get("username", "").strip(), args.get("password", "")
            if not user or not pw:
                raise ValueError("username and password are required")
            if any(c in user + pw for c in "\r\n\0"):
                raise ValueError("invalid characters in credentials")
            _write_json(CREDS_FILE, {"username": user, "password": pw})
            self.has_credentials = True
            self.log("credentials updated")
            return {"username": user}
        if cmd == "get_credentials":
            c = self.credentials()
            return {"username": c["username"]} if c else None
        if cmd == "clear_credentials":
            if os.path.exists(CREDS_FILE):
                os.unlink(CREDS_FILE)
            self.has_credentials = False
            return True
        if cmd == "ping":
            return self.ping_hosts(args.get("hosts", []))
        if cmd == "logs":
            return list(self.logs)[-int(args.get("lines", 500)):]
        raise ValueError(f"unknown command {cmd!r}")

    def update_settings(self, args):
        s = dict(self.settings)
        if "firewall_mode" in args:
            if args["firewall_mode"] not in ("manual", "automatic", "always_on"):
                raise ValueError("bad firewall_mode")
            s["firewall_mode"] = args["firewall_mode"]
        for key in ("allow_lan", "auto_reconnect", "connect_on_boot"):
            if key in args:
                s[key] = bool(args[key])
        if "split_mode" in args:
            if args["split_mode"] not in ("off", "exclusive", "inclusive"):
                raise ValueError("bad split_mode")
            s["split_mode"] = args["split_mode"]
        if "split_list" in args:
            s["split_list"] = [netutil.normalize_cidr(x) for x in args["split_list"] if x.strip()]
        if "custom_dns" in args:
            dns = [x.strip() for x in args["custom_dns"] if x.strip()]
            for ip in dns:
                if not netutil.is_ip(ip) or ":" in ip:
                    raise ValueError(f"invalid IPv4 DNS server: {ip}")
            s["custom_dns"] = dns
        changed_routing = any(s[k] != self.settings[k] for k in ("split_mode", "split_list", "custom_dns"))
        self.settings = s
        self.save_settings()
        self.refresh_firewall()
        if changed_routing and self.state == "connected":
            self.log("routing settings changed; reconnecting")
            server = self.server
            self.want_connected = True
            threading.Thread(target=self._connect_worker, args=(server,), daemon=True).start()
        return self.handle("get_settings", {})

    def ping_hosts(self, hosts):
        results = {}
        bypass = self.state not in ("connected", "reconnecting")

        def one(h):
            try:
                ip = netutil.resolve(h, bypass=bypass)[0]
                results[h] = netutil.ping(ip)
            except Exception:
                results[h] = None
        threads = [threading.Thread(target=one, args=(h,), daemon=True) for h in hosts[:200]]
        for i in range(0, len(threads), 32):
            batch = threads[i:i + 32]
            for t in batch:
                t.start()
            for t in batch:
                t.join(6)
        return results

    # -------------------------------------------------------------- startup
    def startup(self):
        try:
            self.backend.disconnect()        # clean leftovers from a crash
        except Exception as e:
            self.log(f"startup cleanup: {e}")
        self.refresh_firewall()
        last = self.settings.get("last_server")
        if self.settings.get("connect_on_boot") and last and self.credentials():
            threading.Thread(target=self._boot_connect, args=(last,), daemon=True).start()

    def _boot_connect(self, last):
        # wait (up to 2 minutes) for the network to come up after boot
        for _ in range(120):
            if netutil.default_route()[1] or self.stop_event.is_set():
                break
            time.sleep(1)
        if not self.want_connected and self.state == "disconnected":
            self.log(f"connect on boot: {last['hostname']}")
            self.connect(last["hostname"], last.get("name"))

    def shutdown(self):
        self.stop_event.set()
        self.want_connected = False
        try:
            self.backend.disconnect()
        except Exception as e:
            self.log(f"shutdown: {e}")
        self.state = "disconnected"
        if self.settings["firewall_mode"] == "always_on":
            self.log("firewall stays up (Always On)")
            return
        try:
            self.fw.apply(None)
        except Exception as e:
            self.log(f"shutdown firewall: {e}")


# --------------------------------------------------------------------- server
def peer_allowed(conn, daemon):
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", creds)
    if uid == 0 or uid == os.geteuid() or uid in daemon.settings.get("allowed_uids", []):
        return True
    try:
        group = grp.getgrnam("wsike")
    except KeyError:
        return False
    if gid == group.gr_gid:
        return True
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("Groups:"):
                    return str(group.gr_gid) in line.split()[1:]
    except OSError:
        pass
    try:
        return pwd.getpwuid(uid).pw_name in group.gr_mem
    except KeyError:
        return False


def serve_client(conn, daemon):
    with conn:
        try:
            conn.settimeout(30)
            if not peer_allowed(conn, daemon):
                conn.sendall(json.dumps({"ok": False, "error": "permission denied (add your user to the "
                                         "'wsike' group)"}).encode() + b"\n")
                return
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 1 << 20:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            req = json.loads(buf)
            result = daemon.handle(req.get("cmd"), req.get("args") or {})
            reply = {"ok": True, "result": result}
        except Exception as e:
            reply = {"ok": False, "error": str(e)}
        try:
            conn.sendall(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="wsike IKEv2 VPN daemon")
    ap.add_argument("--mock", action="store_true", help="fake backend, no root needed (UI testing)")
    args = ap.parse_args()

    if args.mock:
        from .mock import MockBackend
        backend, fw = MockBackend(), Firewall(mock=True)
    else:
        if os.geteuid() != 0:
            sys.exit("wsike-daemon must run as root (or use --mock)")
        from .backend import StrongSwanBackend
        backend, fw = StrongSwanBackend(log=print), Firewall()

    daemon = Daemon(backend, fw)
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)       # access is checked with SO_PEERCRED
    srv.listen(16)

    def on_signal(*_):
        daemon.log("stopping")
        daemon.shutdown()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        os._exit(0)
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    daemon.log(f"wsike daemon started ({'mock' if args.mock else 'strongSwan'}), socket {SOCKET_PATH}")
    daemon.startup()
    threading.Thread(target=daemon.monitor, daemon=True).start()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve_client, args=(conn, daemon), daemon=True).start()


if __name__ == "__main__":
    main()
