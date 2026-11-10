"""Ruleset generation tests. When `unshare -rn nft` works, rulesets are also
syntax-checked by nft itself inside a throwaway network namespace."""

import itertools
import shutil
import subprocess
import unittest

from wsike.firewall import build_ruleset


def nft_check(script):
    if not shutil.which("unshare") or not shutil.which("nft"):
        return None
    p = subprocess.run(["unshare", "-rn", "nft", "-c", "-f", "-"], input=script,
                       capture_output=True, text=True)
    if "Operation not permitted" in p.stderr:
        return None
    return p


class FirewallTests(unittest.TestCase):
    def test_nothing_when_idle(self):
        self.assertIsNone(build_ruleset(killswitch=False, connected=False, allow_lan=True))

    def test_leak_protection_without_killswitch(self):
        s = build_ruleset(killswitch=False, connected=True, allow_lan=True)
        self.assertIn('oifname != "wsike0" udp dport { 53, 853 } reject', s)
        self.assertIn("meta nfproto ipv6 reject", s)
        self.assertNotIn("chain input", s)

    def test_killswitch_blocks_everything_else(self):
        s = build_ruleset(killswitch=True, connected=False, allow_lan=False, server_ips=["198.51.100.4"])
        self.assertTrue(s.rstrip().endswith("}"))
        out = s.split("chain output")[1].split("chain input")[0]
        self.assertIn("reject with icmpx admin-prohibited", [l for l in out.splitlines() if l.strip()][-2])
        self.assertNotIn("192.168.0.0/16", out)
        self.assertIn("meta mark 0x5753 accept", out)

    def test_bypass_networks(self):
        s = build_ruleset(killswitch=True, connected=True, allow_lan=True, bypass_v4=["203.0.113.7/32"])
        self.assertIn("203.0.113.7/32", s)

    def test_all_combinations_valid_nft(self):
        for ks, conn, lan, bypass in itertools.product([True, False], repeat=4):
            s = build_ruleset(killswitch=ks, connected=conn, allow_lan=lan,
                              server_ips=["198.51.100.4"], bypass_v4=["10.9.0.0/16"] if bypass else [])
            if s is None:
                continue
            p = nft_check(s)
            if p is None:
                self.skipTest("cannot run nft in a user namespace here")
            self.assertEqual(p.returncode, 0, f"{p.stderr}\n{s}")


if __name__ == "__main__":
    unittest.main()
