"""
Minimal QLab 5 OSC client over TCP (standard library only).

QLab listens for OSC on TCP port 53000 and frames packets with SLIP
(OSC 1.1). Queries (and failed commands) get a reply on the address
"/reply/<original address>" whose single string argument is JSON:

    {"workspace_id": "...", "address": "...", "status": "ok", "data": ...}

Successful setters/actions get no reply at all; see QLabClient._send_raw.

Usage:
    q = QLabClient()                 # localhost:53000
    q.connect(passcode="1234")       # picks the front workspace
    cue_id = q.ws("new", "text")     # -> uniqueID of the new cue
    q.cue(cue_id, "text", "Hello")
"""

import json
import socket
import struct

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 53000

# Query that QLab always answers exactly once; used to detect the end of a
# command that produced no reply (see QLabClient._send_raw).
BARRIER_ADDRESS = "/version"

# SLIP framing bytes (RFC 1055)
SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD


class QLabError(Exception):
    pass


# -----------------------------
# OSC encoding / decoding
# -----------------------------
def _osc_string(s):
    b = s.encode("utf-8") + b"\x00"
    return b + b"\x00" * (-len(b) % 4)


def encode_message(address, args=()):
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"
            payload += _osc_string(a)
        else:
            raise TypeError(f"Unsupported OSC argument type: {type(a).__name__}")
    return _osc_string(address) + _osc_string(tags) + payload


def _read_string(data, pos):
    end = data.index(b"\x00", pos)
    s = data[pos:end].decode("utf-8", errors="replace")
    pos = end + 1
    pos += -pos % 4
    return s, pos


def decode_message(data):
    address, pos = _read_string(data, 0)
    if pos >= len(data):
        return address, []
    tags, pos = _read_string(data, pos)
    args = []
    for t in tags[1:]:
        if t == "s":
            s, pos = _read_string(data, pos)
            args.append(s)
        elif t == "i":
            args.append(struct.unpack(">i", data[pos:pos + 4])[0])
            pos += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[pos:pos + 4])[0])
            pos += 4
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        elif t == "N":
            args.append(None)
        else:
            raise QLabError(f"Unsupported OSC type tag in reply: {t}")
    return address, args


def slip_encode(packet):
    out = bytearray([SLIP_END])
    for b in packet:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


def slip_decode(frame):
    out = bytearray()
    esc = False
    for b in frame:
        if esc:
            out.append(SLIP_END if b == SLIP_ESC_END else SLIP_ESC if b == SLIP_ESC_ESC else b)
            esc = False
        elif b == SLIP_ESC:
            esc = True
        else:
            out.append(b)
    return bytes(out)


# -----------------------------
# Client
# -----------------------------
class QLabClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.buffer = bytearray()
        self.workspace_id = None
        self.passcode = None

    # --- transport ---
    def _ensure_socket(self):
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.buffer = bytearray()
            # A fresh TCP connection must re-authenticate with the workspace
            if self.workspace_id is not None:
                self._send_raw(f"/workspace/{self.workspace_id}/connect",
                               [self.passcode] if self.passcode else [])

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _read_frame(self):
        while True:
            # Skip leading END bytes, then look for a complete frame
            while self.buffer and self.buffer[0] == SLIP_END:
                del self.buffer[0]
            end = self.buffer.find(bytes([SLIP_END]))
            if end != -1:
                frame = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                return slip_decode(frame)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("QLab closed the connection")
            self.buffer += chunk

    def _send_raw(self, address, args):
        """
        Send one message and return its parsed reply dict.

        QLab replies to queries (and to failed commands) but stays silent when a
        setter or action (select_id, move, name "x", ...) succeeds. So every
        message is followed by a /version query, which always replies: any reply
        to our message arrives before the /version reply, and if none came the
        command succeeded silently.
        """
        self.sock.sendall(slip_encode(encode_message(address, args))
                          + slip_encode(encode_message(BARRIER_ADDRESS)))
        want = "/reply" + address
        reply = {"status": "ok", "data": None}
        while True:
            reply_addr, reply_args = decode_message(self._read_frame())
            if reply_addr == "/reply" + BARRIER_ADDRESS:
                return reply
            if reply_addr != want:
                continue  # unsolicited update or a stale reply; ignore
            if reply_args and isinstance(reply_args[0], str):
                try:
                    reply = json.loads(reply_args[0])
                except ValueError:
                    reply = {"status": "ok", "data": reply_args[0]}
            else:
                reply = {"status": "ok", "data": reply_args}

    def send(self, address, *args, check=True):
        """
        Send an OSC message and return the reply's "data".
        Retries once on a dropped connection. Raises QLabError on status != ok.
        """
        for attempt in (1, 2):
            try:
                self._ensure_socket()
                reply = self._send_raw(address, list(args))
                break
            except ConnectionError:
                # Dropped connection (e.g. QLab restarted): reconnect once
                self.close()
                if attempt == 2:
                    raise
            except OSError:
                # Timeout etc.: don't retry (the command may have run already)
                self.close()
                raise
        status = reply.get("status", "ok")
        if check and status != "ok":
            raise QLabError(f"{address} {list(args)} -> status '{status}'")
        return reply.get("data")

    # --- workspace helpers ---
    def workspaces(self):
        return self.send("/workspaces")

    def connect(self, workspace_id=None, passcode=None):
        """
        Connect to a workspace by name or uniqueID. With no workspace_id, use the
        first open workspace that grants OSC edit permission (falling back to the
        first one QLab lists).
        Returns {"workspace_id", "display_name", "permissions"}.
        """
        workspaces = self.workspaces() or []
        if not workspaces:
            raise QLabError("No QLab workspace is open")
        if workspace_id is not None:
            matches = [w for w in workspaces
                       if workspace_id in (w.get("uniqueID"), w.get("displayName"))]
            if not matches:
                raise QLabError(f"Workspace '{workspace_id}' not found")
            return self._connect_to(matches[0], passcode)

        for ws in workspaces:
            info = self._connect_to(ws, passcode, raise_badpass=False)
            if "edit" in str(info["permissions"]):
                return info
        # Nothing grants edit: fall back to the first workspace (reads may still work)
        return self._connect_to(workspaces[0], passcode)

    def _connect_to(self, ws, passcode, raise_badpass=True):
        wid = ws["uniqueID"]
        result = self.send(f"/workspace/{wid}/connect", *([passcode] if passcode else []),
                           check=False)
        if raise_badpass and isinstance(result, str) and result.startswith("badpass"):
            raise QLabError(f"QLab rejected the OSC passcode for '{ws.get('displayName')}'")
        self.workspace_id = wid
        self.passcode = passcode
        return {"workspace_id": wid, "display_name": ws.get("displayName"),
                "permissions": result}

    def _require_workspace(self):
        if self.workspace_id is None:
            self.connect()
        return self.workspace_id

    def ws(self, command, *args, check=True):
        """Send /workspace/{id}/{command}."""
        wid = self._require_workspace()
        return self.send(f"/workspace/{wid}/{command}", *args, check=check)

    def cue(self, cue_id, prop, *args, check=True):
        """Send /workspace/{id}/cue_id/{cue_id}/{prop} (get if no args, set otherwise)."""
        return self.ws(f"cue_id/{cue_id}/{prop}", *args, check=check)
