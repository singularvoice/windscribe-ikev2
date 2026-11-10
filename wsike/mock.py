"""Fake backend so the GUI can be developed and tested without root or a VPN."""

import random
import time


class MockBackend:
    def __init__(self):
        self.log = print
        self.server_ip = None
        self.up = False

    def connect(self, hostname, username, password, *, split_mode="off", split_v4=(),
                custom_dns=(), timeout=30):
        self.log(f"[mock] connecting to {hostname} as {username}")
        time.sleep(2)
        if "fail" in hostname:
            raise RuntimeError("Server did not respond (UDP 500/4500 blocked or server down).")
        self.server_ip = f"198.51.100.{random.randint(2, 250)}"
        self.up = True
        return {"server_ip": self.server_ip, "vip": "10.111.0.23",
                "dns": list(custom_dns) or ["10.255.255.1"]}

    def disconnect(self):
        if self.up:
            time.sleep(0.5)
        self.up = False
        self.server_ip = None

    def reinitiate(self, timeout=20):
        self.up = True
        return True

    def setup_dns(self, custom_dns=()):
        pass

    def is_up(self):
        return self.up
