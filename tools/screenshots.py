#!/usr/bin/env python3
"""Render README screenshots of the GUI in each state.

The real MainWindow/PreferencesDialog are driven by a scripted fake daemon,
so every state is reproducible and only demo data (documentation IP ranges,
made-up hostnames) appears in the images.

Needs a display; run it against a throwaway Broadway server so nothing shows
up on your desktop:

    gtk4-broadwayd :9 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:9 python3 tools/screenshots.py docs/screenshots [scene ...]
"""

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Isolated config dir with demo locations (must be set before importing gui).
_cfg = tempfile.mkdtemp(prefix="wsike-shots-")
os.environ["XDG_CONFIG_HOME"] = _cfg
os.makedirs(os.path.join(_cfg, "wsike"))
DEMO_SERVERS = [
    {"name": "Germany - Frankfurt", "hostname": "fra-001-ike.example.net", "country": "de", "favorite": True},
    {"name": "Netherlands - Amsterdam", "hostname": "ams-014-ike.example.net", "country": "nl", "favorite": True},
    {"name": "US East - New York", "hostname": "nyc-207-ike.example.net", "country": "us", "favorite": False},
    {"name": "Canada - Toronto", "hostname": "yyz-033-ike.example.net", "country": "ca", "favorite": False},
    {"name": "United Kingdom - London", "hostname": "lon-102-ike.example.net", "country": "gb", "favorite": False},
    {"name": "Japan - Tokyo", "hostname": "tyo-008-ike.example.net", "country": "jp", "favorite": False},
    {"name": "Switzerland - Zurich", "hostname": "zrh-019-ike.example.net", "country": "ch", "favorite": False},
]
with open(os.path.join(_cfg, "wsike", "servers.json"), "w") as f:
    json.dump(DEMO_SERVERS, f)
with open(os.path.join(_cfg, "wsike", "last.json"), "w") as f:
    json.dump({"hostname": "fra-001-ike.example.net"}, f)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402

from wsike import gui  # noqa: E402

FRA = {"hostname": "fra-001-ike.example.net", "name": "Germany - Frankfurt"}
PINGS = {"fra-001-ike.example.net": 21, "ams-014-ike.example.net": 27, "nyc-207-ike.example.net": 96,
         "yyz-033-ike.example.net": 112, "lon-102-ike.example.net": 34, "tyo-008-ike.example.net": 238,
         "zrh-019-ike.example.net": 29}
BASE = {"state": "disconnected", "error": None, "server": None, "since": None, "server_ip": None,
        "vip": None, "dns": [], "public_ip": None, "firewall": False, "firewall_mode": "automatic",
        "rx": 0, "tx": 0, "has_credentials": True}
SETTINGS = {"firewall_mode": "automatic", "firewall_on": False, "allow_lan": True, "split_mode": "exclusive",
            "split_list": ["192.168.50.0/24", "203.0.113.7", "printer.example.org"], "custom_dns": [],
            "auto_reconnect": True, "connect_on_boot": True, "last_server": FRA}

# (file name, status overrides, extra action)
SCENES = [
    ("off", {}, None),
    ("connecting", {"state": "connecting", "server": FRA, "firewall": True}, None),
    ("on", {"state": "connected", "server": FRA, "firewall": True, "server_ip": "198.51.100.17",
            "vip": "10.183.85.178", "dns": ["10.255.255.3"], "public_ip": "198.51.100.42",
            "rx": 734_003_200, "tx": 52_428_800, "since": "elapsed:5025"}, None),
    ("reconnecting", {"state": "reconnecting", "server": FRA, "firewall": True}, None),
    ("firewall-hold", {"state": "error", "server": FRA, "firewall": True,
                       "error": "Server did not respond (UDP 500/4500 blocked or server down)."}, None),
    ("favourites", {"state": "connected", "server": FRA, "firewall": True, "public_ip": "198.51.100.42",
                    "vip": "10.183.85.178", "dns": ["10.255.255.3"], "server_ip": "198.51.100.17",
                    "rx": 734_003_200, "tx": 52_428_800, "since": "elapsed:5025"}, "favourites"),
    ("preferences-connection", {"state": "connected", "server": FRA, "firewall": True,
                                "since": "elapsed:5025"}, ("prefs", 0)),
    ("preferences-split-tunneling", {"state": "connected", "server": FRA, "firewall": True,
                                     "since": "elapsed:5025"}, ("prefs", 1)),
]

current = dict(BASE)


def fake_request(cmd, timeout=60, **args):
    time.sleep(0.01)
    if cmd == "status":
        st = dict(current)
        if isinstance(st.get("since"), str):
            st["since"] = time.time() - int(st["since"].split(":")[1])
        return st
    if cmd == "ping":
        return {h: PINGS.get(h) for h in args.get("hosts", [])}
    if cmd == "get_settings":
        return SETTINGS
    if cmd == "get_credentials":
        return {"username": "demo-ikev2-user"}
    return True


gui.request = fake_request


def save_png(win, path):
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot.new()
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    rect = Graphene.Rect().init(0, 0, w, h)
    # Render locally with Cairo: Broadway's own renderer ships the nodes to
    # gtk4-broadwayd, which crashes on some libadwaita dialog nodes.
    renderer = Gsk.CairoRenderer.new()
    renderer.realize_for_display(win.get_display())
    texture = renderer.render_texture(node, rect)
    renderer.unrealize()
    texture.save_to_png(path)
    print("wrote", path, f"{texture.get_width()}x{texture.get_height()}")


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
    os.makedirs(out, exist_ok=True)
    app = gui.App()
    # never talk to (or get swallowed by) a running wsike instance on D-Bus
    app.set_flags(app.get_flags() | Gio.ApplicationFlags.NON_UNIQUE)
    only = sys.argv[2:]          # optional scene names to render
    queue = [sc for sc in SCENES if not only or sc[0] in only]
    state = {"prefs": None}

    def step():
        win = app.win
        win.set_size_request(380, 700)
        if state["prefs"]:
            state["prefs"].close()
            state["prefs"] = None
        if not queue:
            app.quit()
            return False
        name, overrides, action = queue.pop(0)
        current.clear()
        current.update(BASE, **overrides)
        win.poll()
        win.refresh_pings()
        tabs = win.search.get_parent()
        all_btn, fav_btn = tabs.get_first_child(), tabs.get_first_child().get_next_sibling()
        (fav_btn if action == "favourites" else all_btn).set_active(True)
        if isinstance(action, tuple) and action[0] == "prefs":
            dlg = gui.PreferencesDialog(win)
            dlg.present(win)
            state["prefs"] = dlg
            state["page"] = action[1]
        # give async callbacks and animations time to settle, then capture
        GLib.timeout_add(1200, lambda: (capture(name), False)[1])
        return False

    def capture(name):
        dlg = state.get("prefs")
        if dlg is not None and state.get("page"):
            # switch to the requested page via the dialog's view switcher
            page = [c for c in walk(dlg) if isinstance(c, Adw.PreferencesPage)][state["page"]]
            dlg.set_visible_page(page)
            state["page"] = 0
            GLib.timeout_add(800, lambda: (capture(name), False)[1])
            return
        save_png(app.win, os.path.join(out, f"{name}.png"))
        GLib.timeout_add(200, step)

    def walk(widget):
        child = widget.get_first_child()
        while child:
            yield child
            yield from walk(child)
            child = child.get_next_sibling()

    def on_activate(*_):
        GLib.timeout_add(1500, step)
    app.connect("activate", on_activate)
    app.run([])


if __name__ == "__main__":
    main()
