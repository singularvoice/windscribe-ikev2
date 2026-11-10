"""Shared constants and the tiny JSON-lines protocol used between GUI/CLI and daemon."""

import json
import os
import socket

SOCKET_PATH = os.environ.get("WSIKE_SOCKET", "/run/wsike/wsike.sock")

# Identifiers used throughout the networking setup.
IFACE = "wsike0"            # XFRM interface carrying tunnel traffic
IF_ID = 0x5753              # XFRM if_id binding the SAs to IFACE
FWMARK = 0x5753             # mark on IKE/ESP outer packets and daemon sockets
ROUTE_TABLE = 5753          # policy routing table that points into the tunnel
RULE_PRIO_EXCLUDE = 5740    # split-tunnel exclusions (lookup main)
RULE_PRIO_FWMARK = 5750     # marked packets use main table
RULE_PRIO_SUPPRESS = 5751   # main table but ignore default route
RULE_PRIO_TUNNEL = 5752     # everything else -> tunnel table
NFT_TABLE = "wsike"
CONN_NAME = "wsike"

STATES = ("disconnected", "connecting", "connected", "reconnecting", "disconnecting", "error")


class DaemonError(Exception):
    pass


def request(cmd, timeout=60, **args):
    """Send one request to the daemon and return the `result` field."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET_PATH)
    except OSError as e:
        s.close()
        raise DaemonError(f"cannot reach wsike daemon at {SOCKET_PATH}: {e}") from e
    try:
        s.sendall(json.dumps({"cmd": cmd, "args": args}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf:
        raise DaemonError("empty reply from daemon")
    reply = json.loads(buf)
    if not reply.get("ok"):
        raise DaemonError(reply.get("error", "unknown error"))
    return reply.get("result")
