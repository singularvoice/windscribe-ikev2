"""wsikectl - command line control for the wsike daemon."""

import argparse
import getpass
import json
import sys
import time

from .common import DaemonError, request


def main():
    ap = argparse.ArgumentParser(prog="wsikectl", description="Control the wsike IKEv2 VPN daemon")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("connect")
    c.add_argument("hostname")
    c.add_argument("--name")
    c.add_argument("--wait", action="store_true", help="wait until connected or failed")
    sub.add_parser("disconnect")
    f = sub.add_parser("firewall")
    f.add_argument("onoff", choices=["on", "off"])
    s = sub.add_parser("set", help="change settings, e.g. set firewall_mode=always_on allow_lan=false")
    s.add_argument("pairs", nargs="+")
    sub.add_parser("settings")
    sub.add_parser("login", help="store IKEv2 credentials")
    sub.add_parser("logs")
    args = ap.parse_args()

    try:
        if args.cmd == "status":
            print(json.dumps(request("status"), indent=2))
        elif args.cmd == "connect":
            request("connect", hostname=args.hostname, name=args.name)
            if args.wait:
                while True:
                    time.sleep(1)
                    st = request("status")
                    if st["state"] in ("connected", "error", "disconnected"):
                        print(st["state"], st.get("error") or "")
                        sys.exit(0 if st["state"] == "connected" else 1)
        elif args.cmd == "disconnect":
            request("disconnect")
        elif args.cmd == "firewall":
            print("firewall:", request("set_firewall", on=args.onoff == "on"))
        elif args.cmd == "settings":
            print(json.dumps(request("get_settings"), indent=2))
        elif args.cmd == "set":
            changes = {}
            for pair in args.pairs:
                k, _, v = pair.partition("=")
                if v.lower() in ("true", "false", "yes", "no", "on", "off"):
                    v = v.lower() in ("true", "yes", "on")
                elif k in ("split_list", "custom_dns"):
                    v = [x for x in v.split(",") if x]
                changes[k] = v
            print(json.dumps(request("set_settings", **changes), indent=2))
        elif args.cmd == "login":
            user = input("IKEv2 username: ").strip()
            pw = getpass.getpass("IKEv2 password: ")
            request("set_credentials", username=user, password=pw)
            print("saved")
        elif args.cmd == "logs":
            print("\n".join(request("logs", lines=2000)))
    except DaemonError as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
