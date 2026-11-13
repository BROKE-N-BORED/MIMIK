"""WebSocket capture + replay (v0.4 item 13). Stdlib only — no new
dependency for the generated clients that replay WS sessions.

What mitmproxy gives us (proven live against mitmproxy 12.3.1, see
~/workspace/mimik-v04-repo-audit/item13-ws-flow.json):

- GET /flows lists a WS flow with response status 101 and a
  "websocket" key — but only message META (count, total length).
- The messages live at GET /flows/<id>/messages/content/<view>.json,
  one {"text", "view_name", "from_client", "timestamp"} per message.
- view "raw" returns text messages EXACTLY, but mangles binary ones
  ("...bin" — non-printable bytes replaced).
- view "auto" renders binary messages as a hex dump (parseable back
  to exact bytes — see parse_hex_dump) but pretty-prints JSON text.

So Mitm.ws_messages() fetches BOTH views: text comes from "raw",
binary from "auto"'s hex dump. A stored message is {"from_client",
"timestamp", "text"} or {"from_client", "timestamp", "b64"}.
"""

import base64
import hashlib
import os
import re
import socket
import ssl
import struct
import time
from urllib.parse import urlparse

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0
OP_TEXT = 1
OP_BINARY = 2
OP_CLOSE = 8
OP_PING = 9
OP_PONG = 10

# After the last message, this long without a reply means the server
# is done talking (replay collection, not a protocol timeout).
REPLY_IDLE_SECONDS = 2.0
DEFAULT_TIMEOUT = 30


# ---- flow inspection -------------------------------------------------------

def is_websocket_flow(flow):
    """True when a flow is a WebSocket session.

    mitmweb's /flows JSON carries a "websocket" key on WS flows. The
    header check covers flows from sources that don't (an Upgrade
    handshake that got a 101 back).
    """
    if "websocket" in flow:
        return True
    req = flow.get("request") or {}
    headers = req.get("headers") or []
    upgrade = False
    for name, value in headers:
        if name.lower() == "upgrade" and "websocket" in value.lower():
            upgrade = True
    resp = flow.get("response") or {}
    return upgrade and resp.get("status_code") == 101


def ws_messages(flow):
    """The saved WS messages for a flow: [{"from_client", "timestamp",
    "text"} | {"from_client", "timestamp", "b64"}]. Empty when the flow
    has none (plain HTTP, or captured before v0.4)."""
    messages = flow.get("_saved_ws_messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict)]


def client_messages(flow):
    """Just the client->server messages, in order (what replay sends)."""
    return [m for m in ws_messages(flow) if m.get("from_client")]


def message_bytes(message):
    """One saved message as (is_text, bytes)."""
    if message.get("b64") is not None:
        try:
            return False, base64.b64decode(message["b64"])
        except (ValueError, TypeError):
            return False, b""
    return True, (message.get("text") or "").encode("utf-8", "replace")


def ws_url(flow):
    """The ws:// or wss:// URL a WS flow was captured against."""
    req = flow.get("request") or {}
    scheme = req.get("scheme") or "https"
    ws_scheme = "wss" if scheme == "https" else "ws"
    host = req.get("host") or ""
    port = req.get("port")
    default = ((ws_scheme == "wss" and port in (443, None))
               or (ws_scheme == "ws" and port in (80, None)))
    port_part = "" if default else f":{port}"
    return f"{ws_scheme}://{host}{port_part}{req.get('path') or '/'}"


def http_to_ws_url(url):
    """http(s)://... -> ws(s)://... (for Session.ws_replay)."""
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def parse_hex_dump(text):
    """mitmproxy's hex-dump view back to bytes, or None if unparseable.

    Lines look like "0000:   00 01 02 62  69 6e  ...bin" — offset, up to
    16 hex bytes in groups of four (single space inside a group,
    double between groups), then the ASCII gutter. The hex area is a
    fixed 50 chars after the offset, so the gutter (which can contain
    hex-looking characters) is never mistaken for bytes.
    """
    out = bytearray()
    for line in text.splitlines():
        match = re.match(r"^[0-9a-fA-F]+:(\s+)", line)
        if not match:
            return None
        # 16 bytes: 16*2 hex chars + 12 single spaces + 3 double
        # spaces between the groups of four = 50 chars of hex area.
        hex_area = line[match.end():match.end() + 50]
        pairs = hex_area.split()
        if not pairs:
            return None
        for pair in pairs:
            if not re.fullmatch(r"[0-9a-fA-F]{2}", pair):
                return None
            out.append(int(pair, 16))
    return bytes(out) if out else None


# ---- minimal WebSocket client (RFC 6455, stdlib only) ---------------------

def _read_exact(sock, n, stash=None):
    """Read exactly n bytes, consuming stash[0] first when given.

    stash is a one-element list holding bytes already read from the
    socket (e.g. frame bytes that arrived with the handshake response).
    """
    buf = b""
    if stash is not None and stash[0]:
        take = stash[0][:n]
        stash[0] = stash[0][n:]
        buf += take
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        buf += chunk
    return buf


def _read_frame(sock, stash=None):
    """One frame -> (opcode, payload). stash: see _read_exact."""
    b1, b2 = _read_exact(sock, 2, stash)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2, stash))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8, stash))[0]
    mask = _read_exact(sock, 4, stash) if masked else None
    payload = _read_exact(sock, length, stash) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_frame(sock, opcode, payload, mask):
    b1 = 0x80 | opcode
    length = len(payload)
    if length < 126:
        header = struct.pack(">BB", b1, (0x80 if mask else 0) | length)
    elif length < 65536:
        header = struct.pack(">BBH", b1, (0x80 if mask else 0) | 126,
                             length)
    else:
        header = struct.pack(">BBQ", b1, (0x80 if mask else 0) | 127,
                             length)
    if mask:
        key = os.urandom(4)
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        sock.sendall(header + key + payload)
    else:
        sock.sendall(header + payload)


class WSClient:
    """A minimal WS client: handshake, send text/binary, read frames.

    Clients MUST mask (servers must not) — send_* mask, reads accept
    either. Ping frames are answered with pongs automatically.
    """

    def __init__(self):
        self.sock = None
        # Bytes read past the end of the handshake response (a frame
        # that arrived in the same packet) — replayed by recv().
        self._stash = [b""]

    def connect(self, url, headers=None, timeout=DEFAULT_TIMEOUT):
        """Open a WS connection. headers are extra handshake headers
        (auth etc.) — the WS headers themselves are set here."""
        parsed = urlparse(url)
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError(f"not a ws:// or wss:// URL: {url}")
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        sock = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1",
                 f"Host: {host}" + (f":{port}" if parsed.port else ""),
                 "Upgrade: websocket",
                 "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}",
                 "Sec-WebSocket-Version: 13"]
        for name, value in (headers or {}).items():
            if name.lower() in ("host", "upgrade", "connection",
                                "sec-websocket-key",
                                "sec-websocket-version"):
                continue
            lines.append(f"{name}: {value}")
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        sock.settimeout(timeout)
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake: connection closed")
            resp += chunk
        # A frame sent with the 101 can arrive in the same packet —
        # keep the bytes past the headers instead of dropping them.
        _head, _sep, rest = resp.partition(b"\r\n\r\n")
        self._stash[0] = rest
        status = resp.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status:
            raise ConnectionError(f"handshake failed: {status}")
        self.sock = sock
        return self

    def send_text(self, text):
        _send_frame(self.sock, OP_TEXT, text.encode("utf-8"), mask=True)

    def send_bytes(self, data):
        _send_frame(self.sock, OP_BINARY, bytes(data), mask=True)

    def recv(self, timeout=None):
        """Next data frame -> (is_text, payload). Returns (None, None)
        on close; raises TimeoutError when nothing arrives in time."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        while True:
            opcode, payload = _read_frame(self.sock, self._stash)
            if opcode == OP_PING:
                _send_frame(self.sock, OP_PONG, payload, mask=True)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    _send_frame(self.sock, OP_CLOSE, b"", mask=True)
                except OSError:
                    pass
                return None, None
            if opcode in (OP_TEXT, OP_BINARY):
                return opcode == OP_TEXT, payload
            # Continuation frames without a start: skip.

    def close(self):
        if self.sock is None:
            return
        try:
            _send_frame(self.sock, OP_CLOSE, b"", mask=True)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---- replay -----------------------------------------------------------------

def replay_messages(url, headers, messages, timeout=DEFAULT_TIMEOUT):
    """Connect, send messages, collect replies until the server goes
    quiet (REPLY_IDLE_SECONDS of silence) or the timeout hits.

    messages: str (text) or bytes (binary). Returns {"ok", "sent",
    "received": [{"text"} | {"b64"}], "error"} — never raises.
    """
    sent = 0
    received = []
    started = time.monotonic()
    client = WSClient()
    try:
        client.connect(url, headers=headers, timeout=timeout)
        for message in messages:
            if isinstance(message, str):
                client.send_text(message)
            else:
                client.send_bytes(message)
            sent += 1
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                break
            try:
                is_text, payload = client.recv(
                    timeout=min(REPLY_IDLE_SECONDS, remaining))
            except (TimeoutError, socket.timeout):
                break
            if is_text is None:
                break
            if is_text:
                received.append(
                    {"text": payload.decode("utf-8", "replace")})
            else:
                received.append(
                    {"b64": base64.b64encode(payload).decode("ascii")})
    except Exception as e:  # noqa: BLE001 — replay reports, never raises
        return {"ok": False, "sent": sent, "received": received,
                "error": str(e) or e.__class__.__name__}
    finally:
        client.close()
    return {"ok": True, "sent": sent, "received": received, "error": None}


def replay_ws(flow, messages=None, timeout=DEFAULT_TIMEOUT):
    """Replay one captured WS flow: re-send its client messages in
    order, collect the server's replies. Returns the replay_messages
    result dict (never raises). ``messages`` overrides the flow's own
    client messages (the GUI passes the endpoint's — live flows don't
    carry theirs in the flow dict)."""
    from . import replay

    if messages is None:
        messages = client_messages(flow)
    send = []
    for message in messages:
        is_text, data = message_bytes(message)
        send.append(data.decode("utf-8") if is_text else data)
    return replay_messages(ws_url(flow), replay.replay_headers(flow),
                           send, timeout=timeout)
