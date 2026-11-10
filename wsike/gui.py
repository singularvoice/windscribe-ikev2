"""wsike GTK4 / libadwaita user interface (unprivileged)."""

import json
import os
import re
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__  # noqa: E402
from .common import DaemonError, request  # noqa: E402

APP_ID = "com.github.wsike"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "wsike")
SERVERS_FILE = os.path.join(CONFIG_DIR, "servers.json")

COUNTRY_NAMES = {
    "us": "United States", "ca": "Canada", "gb": "United Kingdom", "uk": "United Kingdom",
    "de": "Germany", "fr": "France", "nl": "Netherlands", "ch": "Switzerland", "se": "Sweden",
    "no": "Norway", "dk": "Denmark", "fi": "Finland", "es": "Spain", "it": "Italy", "pt": "Portugal",
    "ie": "Ireland", "be": "Belgium", "at": "Austria", "pl": "Poland", "cz": "Czechia", "ro": "Romania",
    "hu": "Hungary", "gr": "Greece", "tr": "Turkey", "jp": "Japan", "kr": "South Korea",
    "hk": "Hong Kong", "sg": "Singapore", "au": "Australia", "nz": "New Zealand", "in": "India",
    "br": "Brazil", "mx": "Mexico", "ar": "Argentina", "za": "South Africa", "ae": "UAE",
    "il": "Israel", "ru": "Russia", "ua": "Ukraine", "is": "Iceland", "lt": "Lithuania",
    "lv": "Latvia", "ee": "Estonia", "bg": "Bulgaria", "rs": "Serbia", "hr": "Croatia",
    "sk": "Slovakia", "si": "Slovenia", "lu": "Luxembourg", "md": "Moldova", "al": "Albania",
    "cy": "Cyprus", "th": "Thailand", "vn": "Vietnam", "id": "Indonesia", "my": "Malaysia",
    "ph": "Philippines", "tw": "Taiwan", "co": "Colombia", "cl": "Chile", "pe": "Peru",
}

CSS = """
:root { --accent-bg-color: #23b85c; --accent-color: #55ff8a; }
window.wsike { background: #0a1424; }
.wsike-header { padding: 14px 16px 18px 16px; border-radius: 0 0 14px 14px;
  background: linear-gradient(170deg, #26303f 0%, #121b29 100%); transition: all 300ms ease; }
.wsike-header.connecting { background: linear-gradient(170deg, #4b3f15 0%, #1b1a17 100%); }
.wsike-header.connected { background: linear-gradient(170deg, #0a5cff 0%, #052c71 100%); }
.wsike-header.error { background: linear-gradient(170deg, #6d1d22 0%, #24121a 100%); }
.wsike-brand { font-weight: 800; font-size: 17px; color: white; letter-spacing: 1px; }
.wsike-state { font-weight: 800; font-size: 28px; color: #c8ced8; }
.wsike-state.connected { color: #55ff8a; }
.wsike-state.connecting { color: #ffd166; }
.wsike-state.error { color: #ff7b85; }
.wsike-badge { font-size: 11px; font-weight: 700; color: #dfe7f3; padding: 2px 7px;
  border-radius: 6px; background: alpha(white, 0.14); }
.wsike-sub { color: alpha(white, 0.75); font-size: 12px; }
.wsike-power { min-width: 76px; min-height: 76px; border-radius: 999px; padding: 0;
  background: #e8ecf2; color: #0b1626; box-shadow: 0 4px 18px alpha(black, 0.45); }
.wsike-power image { -gtk-icon-size: 34px; }
.wsike-power.connected { background: #55ff8a; color: #003318; }
.wsike-power.connecting { background: #ffd166; color: #3d2c00; }
.wsike-location { margin: 12px 12px 0 12px; padding: 12px; border-radius: 12px; background: #13223a; }
.wsike-flag { font-size: 30px; }
.wsike-loc-name { font-size: 17px; font-weight: 700; color: white; }
.wsike-loc-host { font-size: 12px; color: #8ea0bb; }
.wsike-fw-row { margin: 10px 12px 0 12px; padding: 8px 12px; border-radius: 12px; background: #13223a; }
.wsike-fw-label { font-weight: 700; color: white; }
.wsike-ip { font-family: monospace; color: #8ea0bb; font-size: 12px; }
.wsike-list { background: transparent; }
.wsike-list row { padding: 6px 4px; border-radius: 10px; margin: 2px 8px; background: transparent; }
.wsike-list row:hover { background: #172a47; }
.wsike-list row.active { background: #1c3560; }
.wsike-row-flag { font-size: 22px; }
.wsike-row-name { font-weight: 600; color: white; }
.wsike-row-host { font-size: 11px; color: #7d8fa9; }
.wsike-ping { font-size: 11px; font-family: monospace; color: #8ea0bb; min-width: 48px; }
.wsike-ping.good { color: #55ff8a; } .wsike-ping.ok { color: #ffd166; } .wsike-ping.bad { color: #ff7b85; }
.wsike-tabs { margin: 10px 12px 4px 12px; }
.wsike-empty { color: #7d8fa9; padding: 30px; }
.wsike-footer { padding: 6px 12px; color: #7d8fa9; font-size: 11px; }
.wsike-star { color: #4f6280; } .wsike-star.fav { color: #ffd166; }
"""


def flag(cc):
    cc = (cc or "").lower()
    if cc == "uk":
        cc = "gb"
    if len(cc) != 2 or not cc.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in cc)


def guess_country(hostname):
    m = re.match(r"([a-z]{2})(?:[-.\d]|$)", hostname.lower())
    return m.group(1) if m else ""


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def run_async(fn, callback=None, *args, **kwargs):
    """Run fn in a thread, deliver (result, error) to callback on the GTK main loop."""
    def worker():
        try:
            res, err = fn(*args, **kwargs), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        if callback:
            GLib.idle_add(lambda: (callback(res, err), False)[1])
    threading.Thread(target=worker, daemon=True).start()


class ServerStore:
    def __init__(self):
        self.servers = []
        self.load()

    def load(self):
        try:
            with open(SERVERS_FILE) as f:
                self.servers = json.load(f)
        except (OSError, ValueError):
            self.servers = []

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SERVERS_FILE + ".tmp", "w") as f:
            json.dump(self.servers, f, indent=2)
        os.replace(SERVERS_FILE + ".tmp", SERVERS_FILE)

    def add(self, hostname, name=None, country=None):
        hostname = hostname.strip().lower()
        if not hostname:
            return None
        for s in self.servers:
            if s["hostname"] == hostname:
                if name:
                    s["name"] = name
                if country:
                    s["country"] = country
                return s
        cc = (country or guess_country(hostname)).lower()
        s = {"hostname": hostname, "country": cc,
             "name": name or (COUNTRY_NAMES.get(cc, hostname.split(".")[0])), "favorite": False}
        self.servers.append(s)
        return s

    def remove(self, hostname):
        self.servers = [s for s in self.servers if s["hostname"] != hostname]

    def import_text(self, text):
        """Accept lines like `hostname`, `Name, hostname` or `Name, hostname, cc`."""
        added = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in re.split(r"[,;\t]", line) if p.strip()]
            host = next((p for p in parts if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", p)
                         or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p)), None)
            if not host:
                continue
            others = [p for p in parts if p != host]
            cc = next((p for p in others if re.fullmatch(r"[A-Za-z]{2}", p)), None)
            name = next((p for p in others if p != cc), None)
            if self.add(host, name, cc):
                added += 1
        self.save()
        return added


class ServerRow(Gtk.ListBoxRow):
    def __init__(self, server, on_fav, on_delete):
        super().__init__()
        self.server = server
        box = Gtk.Box(spacing=10, margin_start=6, margin_end=6)
        box.append(Gtk.Label(label=flag(server.get("country")), css_classes=["wsike-row-flag"]))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label=server["name"], xalign=0, css_classes=["wsike-row-name"],
                              ellipsize=Pango.EllipsizeMode.END))
        text.append(Gtk.Label(label=server["hostname"], xalign=0, css_classes=["wsike-row-host"],
                              ellipsize=Pango.EllipsizeMode.END))
        box.append(text)
        self.ping_label = Gtk.Label(label="", css_classes=["wsike-ping"], xalign=1)
        box.append(self.ping_label)
        star = Gtk.Button(icon_name="starred-symbolic" if server.get("favorite") else "non-starred-symbolic",
                          css_classes=["flat", "circular", "wsike-star"] + (["fav"] if server.get("favorite") else []),
                          valign=Gtk.Align.CENTER, tooltip_text="Favourite")
        star.connect("clicked", lambda *_: on_fav(server))
        box.append(star)
        menu = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat", "circular"],
                          valign=Gtk.Align.CENTER, tooltip_text="Remove location")
        menu.connect("clicked", lambda *_: on_delete(server))
        box.append(menu)
        self.set_child(box)

    def set_ping(self, ms):
        for c in ("good", "ok", "bad"):
            self.ping_label.remove_css_class(c)
        if ms is None:
            self.ping_label.set_label("—")
            return
        self.ping_label.set_label(f"{ms} ms")
        self.ping_label.add_css_class("good" if ms < 80 else "ok" if ms < 180 else "bad")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="wsike", default_width=380, default_height=700)
        self.add_css_class("wsike")
        self.store = ServerStore()
        self.status = {"state": "disconnected"}
        self.pings = {}
        self.selected = None
        self.daemon_ok = None
        self.only_favs = False
        self._fw_updating = False

        self.toast = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast.set_child(root)
        self.set_content(self.toast)

        # ---- header ---------------------------------------------------------
        self.header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["wsike-header"])
        top = Gtk.Box(spacing=6)
        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic", css_classes=["flat", "circular"])
        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Add location…", "app.add-server")
        menu.append("Import locations…", "app.import")
        menu.append("Debug log", "app.logs")
        menu.append("About", "app.about")
        menu.append("Quit", "app.quit")
        menu_btn.set_menu_model(menu)
        top.append(menu_btn)
        top.append(Gtk.Label(label="WSIKE", css_classes=["wsike-brand"], hexpand=True, xalign=0))
        # window controls (we have no titlebar of our own)
        close = Gtk.Button(icon_name="window-close-symbolic", css_classes=["flat", "circular"])
        close.connect("clicked", lambda *_: self.close())
        mini = Gtk.Button(icon_name="window-minimize-symbolic", css_classes=["flat", "circular"])
        mini.connect("clicked", lambda *_: self.minimize())
        top.append(mini)
        top.append(close)
        handle = Gtk.WindowHandle(child=top)
        self.header.append(handle)

        mid = Gtk.Box(spacing=12, margin_top=10)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER, spacing=4)
        state_line = Gtk.Box(spacing=8)
        self.state_label = Gtk.Label(label="OFF", css_classes=["wsike-state"], xalign=0)
        state_line.append(self.state_label)
        badge_box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        badge_box.append(Gtk.Label(label="IKEv2", css_classes=["wsike-badge"]))
        badge_box.append(Gtk.Label(label="500/4500", css_classes=["wsike-badge"]))
        state_line.append(badge_box)
        left.append(state_line)
        self.sub_label = Gtk.Label(label="", css_classes=["wsike-sub"], xalign=0, wrap=True,
                                   wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=30)
        left.append(self.sub_label)
        mid.append(left)
        self.power = Gtk.Button(css_classes=["wsike-power"], valign=Gtk.Align.CENTER,
                                tooltip_text="Connect / disconnect")
        self.power.set_child(Gtk.Image(icon_name="system-shutdown-symbolic"))
        self.power.connect("clicked", self.on_power)
        mid.append(self.power)
        self.header.append(mid)
        root.append(self.header)

        # ---- selected location ------------------------------------------------
        loc = Gtk.Box(spacing=12, css_classes=["wsike-location"])
        self.loc_flag = Gtk.Label(label="🌐", css_classes=["wsike-flag"])
        loc.append(self.loc_flag)
        lt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.loc_name = Gtk.Label(label="No location selected", xalign=0, css_classes=["wsike-loc-name"],
                                  ellipsize=Pango.EllipsizeMode.END)
        self.loc_host = Gtk.Label(label="Add a server from the IKEv2 config generator", xalign=0,
                                  css_classes=["wsike-loc-host"], ellipsize=Pango.EllipsizeMode.END)
        lt.append(self.loc_name)
        lt.append(self.loc_host)
        loc.append(lt)
        root.append(loc)

        fw = Gtk.Box(spacing=10, css_classes=["wsike-fw-row"])
        fw.append(Gtk.Image(icon_name="security-high-symbolic"))
        fwt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        fwt.append(Gtk.Label(label="Firewall", xalign=0, css_classes=["wsike-fw-label"]))
        self.fw_mode_label = Gtk.Label(label="", xalign=0, css_classes=["wsike-row-host"])
        fwt.append(self.fw_mode_label)
        fw.append(fwt)
        self.ip_label = Gtk.Label(label="", css_classes=["wsike-ip"])
        fw.append(self.ip_label)
        self.fw_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.fw_switch.connect("state-set", self.on_firewall)
        fw.append(self.fw_switch)
        root.append(fw)

        # ---- location list --------------------------------------------------
        tabs = Gtk.Box(spacing=6, css_classes=["wsike-tabs"])
        all_btn = Gtk.ToggleButton(label="Locations", active=True, css_classes=["flat"])
        fav_btn = Gtk.ToggleButton(label="Favourites", group=all_btn, css_classes=["flat"])
        all_btn.connect("toggled", lambda b: self.set_fav_filter(not b.get_active()))
        tabs.append(all_btn)
        tabs.append(fav_btn)
        self.search = Gtk.SearchEntry(placeholder_text="Search", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        tabs.append(self.search)
        add = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat", "circular"],
                         tooltip_text="Add location")
        add.connect("clicked", lambda *_: self.add_server_dialog())
        tabs.append(add)
        ping_btn = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat", "circular"],
                              tooltip_text="Measure latency")
        ping_btn.connect("clicked", lambda *_: self.refresh_pings())
        tabs.append(ping_btn)
        root.append(tabs)

        self.listbox = Gtk.ListBox(css_classes=["wsike-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_filter_func(self.filter_row)
        self.listbox.connect("row-activated", self.on_row_activated)
        placeholder = Gtk.Label(label="No locations yet.\nUse + to add the server hostname(s)\n"
                                      "from Windscribe's IKEv2 config generator.",
                                justify=Gtk.Justification.CENTER, css_classes=["wsike-empty"])
        self.listbox.set_placeholder(placeholder)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.listbox)
        root.append(scroller)

        self.footer = Gtk.Label(label="", xalign=0, css_classes=["wsike-footer"],
                                ellipsize=Pango.EllipsizeMode.END)
        root.append(self.footer)

        self.rebuild_list()
        last = self.load_last_selected()
        if last:
            self.select_server(last)
        self.poll()
        GLib.timeout_add(1000, self.poll)
        GLib.timeout_add_seconds(2, lambda: (self.refresh_pings(), False)[1])

    # ------------------------------------------------------------------ list
    def rebuild_list(self):
        self.listbox.remove_all()
        servers = sorted(self.store.servers, key=lambda s: (not s.get("favorite"), s["name"].lower()))
        for s in servers:
            row = ServerRow(s, self.toggle_fav, self.confirm_delete)
            row.set_ping(self.pings.get(s["hostname"]))
            if self.selected and s["hostname"] == self.selected["hostname"]:
                row.add_css_class("active")
            self.listbox.append(row)

    def filter_row(self, row):
        s = row.server
        if self.only_favs and not s.get("favorite"):
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in s["name"].lower() or q in s["hostname"]

    def set_fav_filter(self, favs):
        self.only_favs = favs
        self.listbox.invalidate_filter()

    def toggle_fav(self, server):
        server["favorite"] = not server.get("favorite")
        self.store.save()
        self.rebuild_list()

    def confirm_delete(self, server):
        dlg = Adw.AlertDialog(heading="Remove location?", body=f"{server['name']}\n{server['hostname']}")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("remove", "Remove")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)

        def done(d, res):
            if d.choose_finish(res) == "remove":
                self.store.remove(server["hostname"])
                self.store.save()
                self.rebuild_list()
        dlg.choose(self, None, done)

    def on_row_activated(self, _lb, row):
        self.select_server(row.server)
        st = self.status.get("state")
        current = (self.status.get("server") or {}).get("hostname")
        if st in ("connected", "connecting", "reconnecting") and current == row.server["hostname"]:
            return
        self.do_connect()

    def select_server(self, server):
        self.selected = server
        self.loc_flag.set_label(flag(server.get("country")))
        self.loc_name.set_label(server["name"])
        self.loc_host.set_label(server["hostname"])
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(os.path.join(CONFIG_DIR, "last.json"), "w") as f:
                json.dump({"hostname": server["hostname"]}, f)
        except OSError:
            pass
        self.rebuild_list()

    def load_last_selected(self):
        try:
            with open(os.path.join(CONFIG_DIR, "last.json")) as f:
                host = json.load(f)["hostname"]
            return next((s for s in self.store.servers if s["hostname"] == host), None)
        except (OSError, ValueError, KeyError):
            return self.store.servers[0] if self.store.servers else None

    def refresh_pings(self):
        hosts = [s["hostname"] for s in self.store.servers]
        if not hosts or not self.daemon_ok:
            return

        def done(res, err):
            if err or not res:
                return
            self.pings.update(res)
            child = self.listbox.get_first_child()
            while child:
                if isinstance(child, ServerRow):
                    child.set_ping(self.pings.get(child.server["hostname"]))
                child = child.get_next_sibling()
        run_async(lambda: request("ping", timeout=60, hosts=hosts), done)

    # ---------------------------------------------------------------- daemon
    def poll(self):
        run_async(lambda: request("status", timeout=5), self.on_status)
        return True

    def on_status(self, st, err):
        if err:
            self.daemon_ok = False
            self.apply_state("error", "Daemon not reachable: " + str(err).split(": ", 1)[-1])
            return
        if self.daemon_ok is False or self.daemon_ok is None:
            self.daemon_ok = True
            if not st.get("has_credentials"):
                self.show_toast("Add your IKEv2 credentials in Preferences → Account")
        self.status = st
        state = st["state"]
        server = st.get("server") or {}
        if state in ("connected", "connecting", "reconnecting") and server.get("hostname"):
            known = next((s for s in self.store.servers if s["hostname"] == server["hostname"]), None)
            if known and (not self.selected or self.selected["hostname"] != known["hostname"]):
                self.select_server(known)
        sub = ""
        if state == "connected":
            since = st.get("since") or time.time()
            dur = int(time.time() - since)
            sub = (f"{dur // 3600:02d}:{dur % 3600 // 60:02d}:{dur % 60:02d}  ·  "
                   f"↓ {human_bytes(st.get('rx', 0))}  ↑ {human_bytes(st.get('tx', 0))}")
        elif state == "error":
            sub = st.get("error") or "Error"
        elif state == "reconnecting":
            sub = "Connection lost, reconnecting… (traffic blocked)" if st.get("firewall") else \
                "Connection lost, reconnecting…"
        self.apply_state(state, sub)

        self._fw_updating = True
        self.fw_switch.set_active(bool(st.get("firewall")))
        self.fw_switch.set_sensitive(st.get("firewall_mode") != "always_on")
        self._fw_updating = False
        self.fw_mode_label.set_label({"manual": "Manual", "automatic": "Automatic",
                                      "always_on": "Always on"}.get(st.get("firewall_mode"), ""))
        ip = st.get("public_ip") if state == "connected" else None
        self.ip_label.set_label(ip or (st.get("vip") or "") if state == "connected" else "")
        if state == "connected":
            dns = ", ".join(st.get("dns") or [])
            self.footer.set_label(f"Server {st.get('server_ip')}  ·  Tunnel IP {st.get('vip')}  ·  DNS {dns}")
        else:
            self.footer.set_label(f"wsike {__version__}")

    def apply_state(self, state, sub=""):
        labels = {"disconnected": "OFF", "connecting": "CONNECTING", "connected": "ON",
                  "reconnecting": "RECONNECTING", "disconnecting": "DISCONNECTING", "error": "OFF"}
        css = {"connected": "connected", "connecting": "connecting", "reconnecting": "connecting",
               "disconnecting": "connecting", "error": "error"}.get(state)
        for w in (self.header, self.state_label, self.power):
            for c in ("connected", "connecting", "error"):
                w.remove_css_class(c)
            if css:
                w.add_css_class(css)
        self.state_label.set_label(labels.get(state, state.upper()))
        self.sub_label.set_label(sub)
        self.sub_label.set_visible(bool(sub))

    def on_power(self, *_):
        if self.status.get("state") in ("connected", "connecting", "reconnecting"):
            run_async(lambda: request("disconnect"), self.on_cmd_done)
        else:
            self.do_connect()

    def do_connect(self):
        if not self.selected:
            self.show_toast("Add a location first (+)")
            return
        s = self.selected
        self.apply_state("connecting", f"Connecting to {s['name']}…")
        run_async(lambda: request("connect", hostname=s["hostname"], name=s["name"]), self.on_cmd_done)

    def on_cmd_done(self, _res, err):
        if err:
            self.show_toast(str(err))
        self.poll()

    def on_firewall(self, _sw, active):
        if self._fw_updating:
            return False
        run_async(lambda: request("set_firewall", on=active), self.on_cmd_done)
        return False

    def show_toast(self, text):
        self.toast.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=5))

    # ---------------------------------------------------------------- dialogs
    def add_server_dialog(self):
        dlg = Adw.AlertDialog(heading="Add location",
                              body="Enter the server hostname shown by Windscribe's IKEv2 config "
                                   "generator (windscribe.com/getconfig/ikev2).")
        box = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        host = Adw.EntryRow(title="Hostname")
        name = Adw.EntryRow(title="Display name (optional)")
        cc = Adw.EntryRow(title="Country code, e.g. de (optional)")
        for r in (host, name, cc):
            box.append(r)
        dlg.set_extra_child(box)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("add", "Add")
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("add")

        def done(d, res):
            if d.choose_finish(res) != "add":
                return
            h = host.get_text().strip()
            if not re.fullmatch(r"[A-Za-z0-9.-]+", h or "-") or not h:
                self.show_toast("Invalid hostname")
                return
            s = self.store.add(h, name.get_text().strip() or None, cc.get_text().strip().lower() or None)
            self.store.save()
            self.select_server(s)
            self.refresh_pings()
        dlg.choose(self, None, done)

    def import_dialog(self):
        dlg = Adw.AlertDialog(heading="Import locations",
                              body="One server per line: `hostname`, `Name, hostname` or "
                                   "`Name, hostname, country-code`.")
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True,
                            top_margin=6, bottom_margin=6, left_margin=6, right_margin=6)
        sc = Gtk.ScrolledWindow(min_content_height=180, min_content_width=320, child=view)
        dlg.set_extra_child(sc)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("import", "Import")
        dlg.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)

        def done(d, res):
            if d.choose_finish(res) != "import":
                return
            buf = view.get_buffer()
            n = self.store.import_text(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
            self.rebuild_list()
            self.show_toast(f"Imported {n} location(s)")
            self.refresh_pings()
        dlg.choose(self, None, done)

    def logs_dialog(self):
        win = Adw.Window(transient_for=self, modal=True, title="Debug log", default_width=720,
                         default_height=480)
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=8, right_margin=8, top_margin=8, bottom_margin=8)
        sc = Gtk.ScrolledWindow(vexpand=True, child=view)
        tv.set_content(sc)
        win.set_content(tv)

        def done(res, err):
            view.get_buffer().set_text("\n".join(res) if res else str(err))
            GLib.idle_add(lambda: (sc.get_vadjustment().set_value(sc.get_vadjustment().get_upper()), False)[1])
        run_async(lambda: request("logs", lines=1000), done)
        win.present()


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences")
        self.win = win
        self.settings = {}
        self.loading = True

        # General / connection --------------------------------------------
        conn = Adw.PreferencesPage(title="Connection", icon_name="network-vpn-symbolic")
        fw_grp = Adw.PreferencesGroup(title="Firewall",
                                      description="Blocks all traffic outside the tunnel so nothing leaks "
                                                  "if the VPN drops.")
        self.fw_mode = Adw.ComboRow(title="Firewall mode",
                                    model=Gtk.StringList.new(["Manual", "Automatic", "Always on"]))
        self.fw_mode.set_subtitle("Automatic: on while connected · Always on: blocks even when "
                                  "disconnected and after reboot")
        self.allow_lan = Adw.SwitchRow(title="Allow LAN traffic",
                                       subtitle="Printers, NAS, Chromecast… stay reachable")
        fw_grp.add(self.fw_mode)
        fw_grp.add(self.allow_lan)
        conn.add(fw_grp)

        beh = Adw.PreferencesGroup(title="Behaviour")
        self.auto_reconnect = Adw.SwitchRow(title="Auto reconnect", subtitle="Re-establish the tunnel when it drops")
        self.on_boot = Adw.SwitchRow(title="Connect on boot", subtitle="Connect to the last location when the "
                                                                       "computer starts")
        beh.add(self.auto_reconnect)
        beh.add(self.on_boot)
        conn.add(beh)

        dns_grp = Adw.PreferencesGroup(title="DNS",
                                       description="DNS always goes through the tunnel; plain DNS to any other "
                                                   "interface is blocked while connected.")
        self.custom_dns = Adw.EntryRow(title="Custom DNS servers (comma separated, empty = Windscribe DNS)")
        self.custom_dns.set_show_apply_button(True)
        dns_grp.add(self.custom_dns)
        conn.add(dns_grp)
        self.add(conn)

        # Split tunneling ---------------------------------------------------
        split = Adw.PreferencesPage(title="Split Tunneling", icon_name="network-workgroup-symbolic")
        sg = Adw.PreferencesGroup(title="Split tunneling",
                                  description="Exclusive: listed destinations bypass the VPN. "
                                              "Inclusive: only listed destinations use the VPN "
                                              "(the firewall cannot be used in inclusive mode).")
        self.split_mode = Adw.ComboRow(title="Mode", model=Gtk.StringList.new(["Off", "Exclusive", "Inclusive"]))
        sg.add(self.split_mode)
        split.add(sg)
        lg = Adw.PreferencesGroup(title="IPs, subnets and hostnames",
                                  description="One per line, e.g. 192.168.50.0/24, 203.0.113.7, example.com. "
                                              "Hostnames are resolved when connecting.")
        self.split_view = Gtk.TextView(monospace=True, top_margin=8, bottom_margin=8, left_margin=8,
                                       right_margin=8, css_classes=["card"])
        frame = Gtk.ScrolledWindow(min_content_height=200, child=self.split_view)
        lg.add(frame)
        save = Gtk.Button(label="Save list", halign=Gtk.Align.END, margin_top=8, css_classes=["suggested-action"])
        save.connect("clicked", self.save_split_list)
        lg.add(save)
        split.add(lg)
        self.add(split)

        # Account -----------------------------------------------------------
        acc = Adw.PreferencesPage(title="Account", icon_name="avatar-default-symbolic")
        ag = Adw.PreferencesGroup(title="IKEv2 credentials",
                                  description="Generate them at windscribe.com/getconfig/ikev2. They are "
                                              "different from your Windscribe login and are stored by the "
                                              "daemon in /etc/wsike (root only).")
        self.user = Adw.EntryRow(title="Username")
        self.password = Adw.PasswordEntryRow(title="Password")
        ag.add(self.user)
        ag.add(self.password)
        btn = Gtk.Button(label="Save credentials", halign=Gtk.Align.END, margin_top=8,
                         css_classes=["suggested-action"])
        btn.connect("clicked", self.save_creds)
        ag.add(btn)
        acc.add(ag)
        self.add(acc)

        for row in (self.fw_mode, self.split_mode):
            row.connect("notify::selected", self.on_changed)
        for row in (self.allow_lan, self.auto_reconnect, self.on_boot):
            row.connect("notify::active", self.on_changed)
        self.custom_dns.connect("apply", self.on_dns_apply)

        run_async(lambda: (request("get_settings"), request("get_credentials")), self.on_loaded)

    def on_loaded(self, res, err):
        if err:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(err))))
            return
        s, creds = res
        self.settings = s
        self.fw_mode.set_selected(["manual", "automatic", "always_on"].index(s["firewall_mode"]))
        self.allow_lan.set_active(s["allow_lan"])
        self.auto_reconnect.set_active(s["auto_reconnect"])
        self.on_boot.set_active(s["connect_on_boot"])
        self.split_mode.set_selected(["off", "exclusive", "inclusive"].index(s["split_mode"]))
        self.split_view.get_buffer().set_text("\n".join(s.get("split_list", [])))
        self.custom_dns.set_text(", ".join(s.get("custom_dns", [])))
        if creds:
            self.user.set_text(creds["username"])
            self.password.set_text("")
        self.loading = False

    def push(self, **changes):
        def done(res, err):
            if err:
                self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(err))))
            else:
                self.settings = res
                self.add_toast(Adw.Toast(title="Saved", timeout=1))
            self.win.poll()
        run_async(lambda: request("set_settings", **changes), done)

    def on_changed(self, *_):
        if self.loading:
            return
        self.push(firewall_mode=["manual", "automatic", "always_on"][self.fw_mode.get_selected()],
                  allow_lan=self.allow_lan.get_active(),
                  auto_reconnect=self.auto_reconnect.get_active(),
                  connect_on_boot=self.on_boot.get_active(),
                  split_mode=["off", "exclusive", "inclusive"][self.split_mode.get_selected()])

    def on_dns_apply(self, *_):
        self.push(custom_dns=[x.strip() for x in self.custom_dns.get_text().split(",")])

    def save_split_list(self, *_):
        buf = self.split_view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self.push(split_list=[line.strip() for line in text.splitlines() if line.strip()])

    def save_creds(self, *_):
        user, pw = self.user.get_text().strip(), self.password.get_text()
        if not user or not pw:
            self.add_toast(Adw.Toast(title="Enter both username and password"))
            return

        def done(_res, err):
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(err)) if err else "Credentials saved"))
            if not err:
                self.password.set_text("")
        run_async(lambda: request("set_credentials", username=user, password=pw), done)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.win = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb, accel in (
                ("preferences", lambda *_: PreferencesDialog(self.win).present(self.win), "<Ctrl>comma"),
                ("add-server", lambda *_: self.win.add_server_dialog(), "<Ctrl>n"),
                ("import", lambda *_: self.win.import_dialog(), "<Ctrl>i"),
                ("logs", lambda *_: self.win.logs_dialog(), "<Ctrl>l"),
                ("about", self.on_about, None),
                ("quit", lambda *_: self.quit(), "<Ctrl>q")):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
            if accel:
                self.set_accels_for_action(f"app.{name}", [accel])

    def do_activate(self):
        if not self.win:
            self.win = MainWindow(self)
        self.win.present()

    def on_about(self, *_):
        Adw.AboutDialog(application_name="wsike", version=__version__,
                        comments="IKEv2 client for Windscribe with firewall, DNS/IPv6 leak protection "
                                 "and split tunneling. Not affiliated with Windscribe.",
                        application_icon="network-vpn", license_type=Gtk.License.MIT_X11).present(self.win)


def main():
    sys.exit(App().run(sys.argv))


if __name__ == "__main__":
    main()
