"""Daemon state machine tests against the mock backend (no root needed)."""

import os
import tempfile
import threading
import time
import unittest

_tmp = tempfile.mkdtemp(prefix="wsike-test-")
os.environ["WSIKE_STATE_DIR"] = _tmp
os.environ["WSIKE_DNS_CACHE"] = os.path.join(_tmp, "dns.json")

from wsike import daemon as d  # noqa: E402
from wsike.mock import MockBackend  # noqa: E402


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class DaemonTests(unittest.TestCase):
    def setUp(self):
        for f in (d.SETTINGS_FILE, d.CREDS_FILE):
            if os.path.exists(f):
                os.unlink(f)
        self.dm = d.Daemon(MockBackend(), d.Firewall(mock=True), log_to_stderr=False)
        self.dm.handle("set_credentials", {"username": "u", "password": "p"})
        self.dm.handle("set_settings", {"auto_reconnect": False})

    def tearDown(self):
        self.dm.stop_event.set()

    def connect(self, host="de.example.com"):
        self.dm.connect(host)
        self.assertTrue(wait_for(lambda: self.dm.state == "connected"
                                 and (self.dm.server or {}).get("hostname") == host
                                 and self.dm.info.get("vip")), self.dm.state)

    def test_automatic_firewall_follows_user_disconnect(self):
        self.connect()
        self.assertTrue(self.dm.status()["firewall"])
        self.dm.disconnect()
        self.assertTrue(wait_for(lambda: self.dm.state == "disconnected"))
        self.assertFalse(self.dm.status()["firewall"])
        self.assertIsNone(self.dm.fw.last)

    def test_firewall_stays_up_after_unexpected_loss(self):
        self.connect()
        threading.Thread(target=self.dm.monitor, daemon=True).start()
        self.dm.backend.up = False                     # tunnel dies
        self.assertTrue(wait_for(lambda: self.dm.state == "error"))
        self.assertTrue(self.dm.status()["firewall"], "kill switch must keep blocking")
        self.assertIn("reject with icmpx", self.dm.fw.last)
        self.dm.handle("set_firewall", {"on": False})   # explicit user action releases it
        self.assertFalse(self.dm.status()["firewall"])

    def test_disconnect_from_error_releases_hold(self):
        self.connect()
        threading.Thread(target=self.dm.monitor, daemon=True).start()
        self.dm.backend.up = False
        self.assertTrue(wait_for(lambda: self.dm.state == "error"))
        self.dm.disconnect()
        self.assertEqual(self.dm.state, "disconnected")
        self.assertFalse(self.dm.status()["firewall"])

    def test_failed_first_connect_does_not_hold_firewall(self):
        self.dm.connect("fail.example.com")
        self.assertTrue(wait_for(lambda: self.dm.state == "error"))
        self.assertFalse(self.dm.status()["firewall"])

    def test_cancel_while_connecting_is_not_an_error(self):
        self.dm.connect("de.example.com")
        self.assertTrue(wait_for(lambda: self.dm.state == "connecting"))
        self.dm.disconnect()
        self.assertTrue(wait_for(lambda: self.dm.state == "disconnected"), self.dm.state)
        self.assertIsNone(self.dm.error)

    def test_timer_resets_when_switching_servers(self):
        self.connect()
        self.dm.since -= 3600
        self.connect("us.example.com")
        self.assertLess(time.time() - self.dm.since, 60)

    def test_reinitiate_updates_tunnel_info(self):
        self.dm.handle("set_settings", {"auto_reconnect": True})
        self.connect()
        threading.Thread(target=self.dm.monitor, daemon=True).start()
        self.dm.backend.up = False
        self.assertTrue(wait_for(lambda: self.dm.state == "reconnecting", 5))
        self.assertTrue(wait_for(lambda: self.dm.state == "connected", 12))
        self.assertEqual(self.dm.status()["vip"], "10.111.0.24")

    def test_credentials_flag(self):
        self.assertTrue(self.dm.status()["has_credentials"])
        self.dm.handle("clear_credentials", {})
        self.assertFalse(self.dm.status()["has_credentials"])


if __name__ == "__main__":
    unittest.main()
