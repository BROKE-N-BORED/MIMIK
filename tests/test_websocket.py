"""v0.4 item 13: WebSocket capture + replay, vs a local echo server.

The flow + message fixtures are REAL mitmweb output (mitmproxy 12.3.1,
captured live by ~/workspace/mimik-v04-repo-audit/item13-ws-flow.json):
a WS session through the proxy to a local echo server.
"""

import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from mimik import websocket as ws_mod
from mimik.sources import mitm

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


WS_FLOW = _load("ws-flow.json")[0]
WS_AUTO = _load("ws-messages-auto.json")
WS_RAW = _load("ws-messages-raw.json")


class EchoServer:
    """A minimal WS echo server (stdlib only) for the replay tests."""

    def __init__(self, ping_first=False, greeting=None):
        self.ping_first = ping_first
        # greeting: a text frame sent in the SAME sendall as the 101 —
        # proves the client's handshake read doesn't swallow it.
        self.greeting = greeting
        self.pong_seen = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=5)
        self.sock.close()
        return False

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def _handle(self, conn):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            # A frame sent with the handshake can arrive in the same
            # packet — keep the bytes past the headers (same bug the
            # client had: a swallowed first frame).
            _head, _sep, rest = req.partition(b"\r\n\r\n")
            stash = [rest]
            key = re.search(rb"Sec-WebSocket-Key: (\S+)", req).group(1)
            accept = base64.b64encode(hashlib.sha1(
                key + ws_mod.WS_GUID.encode()).digest()).decode()
            reply = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            if self.greeting is not None:
                text = self.greeting.encode()
                reply += struct.pack(">BB", 0x81, len(text)) + text
            conn.sendall(reply)
            conn.settimeout(30)
            if self.ping_first:
                ws_mod._send_frame(conn, ws_mod.OP_PING, b"p", mask=False)
                # The client's text may arrive before its pong —
                # echo data frames until the pong shows up.
                while True:
                    opcode, payload = ws_mod._read_frame(conn, stash)
                    if opcode == ws_mod.OP_PONG:
                        self.pong_seen.append((opcode, payload))
                        break
                    if opcode in (ws_mod.OP_TEXT, ws_mod.OP_BINARY):
                        ws_mod._send_frame(conn, opcode, payload,
                                           mask=False)
                    elif opcode == ws_mod.OP_CLOSE:
                        ws_mod._send_frame(conn, ws_mod.OP_CLOSE, b"",
                                           mask=False)
                        return
            while True:
                opcode, payload = ws_mod._read_frame(conn, stash)
                if opcode == ws_mod.OP_CLOSE:
                    ws_mod._send_frame(conn, ws_mod.OP_CLOSE, b"",
                                       mask=False)
                    break
                if opcode == ws_mod.OP_PING:
                    ws_mod._send_frame(conn, ws_mod.OP_PONG, payload,
                                       mask=False)
                    continue
                ws_mod._send_frame(conn, opcode, payload, mask=False)
        except (ConnectionError, socket.timeout, OSError):
            pass
        finally:
            conn.close()


class WSClientTests(unittest.TestCase):
    def test_text_and_binary_echo(self):
        with EchoServer() as server:
            client = ws_mod.WSClient()
            client.connect(f"ws://127.0.0.1:{server.port}/chat",
                           timeout=10)
            client.send_text("hello-ws")
            self.assertEqual(client.recv(timeout=5), (True, b"hello-ws"))
            client.send_bytes(b"\x00\x01\x02bin")
            self.assertEqual(client.recv(timeout=5),
                             (False, b"\x00\x01\x02bin"))
            client.close()

    def test_client_answers_server_ping(self):
        with EchoServer(ping_first=True) as server:
            client = ws_mod.WSClient()
            client.connect(f"ws://127.0.0.1:{server.port}/chat",
                           timeout=10)
            client.send_text("after-ping")
            self.assertEqual(client.recv(timeout=5),
                             (True, b"after-ping"))
            client.close()
        # The pong lands on the server's handler thread — give it a
        # moment to read it before asserting (15 s: the sandbox runs
        # several suites at once and threads can starve for seconds).
        deadline = time.time() + 15
        while not server.pong_seen and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(server.pong_seen, [(ws_mod.OP_PONG, b"p")])

    def test_frame_sent_with_handshake_is_not_swallowed(self):
        # Regression: connect() read the 101 with recv(4096) and threw
        # away bytes past the headers — a frame in the same packet
        # (e.g. an immediate ping) vanished, so the client never
        # answered it. The greeting rides in the 101's own sendall.
        with EchoServer(greeting="welcome") as server:
            client = ws_mod.WSClient()
            client.connect(f"ws://127.0.0.1:{server.port}/chat",
                           timeout=10)
            self.assertEqual(client.recv(timeout=5), (True, b"welcome"))
            client.close()

    def test_handshake_failure_raises(self):
        with EchoServer() as server:
            # Plain HTTP to the echo server's port never upgrades —
            # connect to a closed port instead: refused, not a 101.
            client = ws_mod.WSClient()
            with self.assertRaises(Exception):
                client.connect("ws://127.0.0.1:9/chat", timeout=2)

    def test_non_ws_url_rejected(self):
        client = ws_mod.WSClient()
        with self.assertRaises(ValueError):
            client.connect("http://127.0.0.1/chat", timeout=2)


class HexDumpTests(unittest.TestCase):
    def test_single_line_from_the_live_probe(self):
        dump = ("0000:   00 01 02 62  69 6e"
                "                                   ...bin")
        self.assertEqual(ws_mod.parse_hex_dump(dump), b"\x00\x01\x02bin")

    def test_multi_line_from_the_live_probe(self):
        dump = ("0000:   00 01 02 03  04 05 06 07  08 09 0a 0b  "
                "0c 0d 0e 0f   ................\n"
                "0010:   10 11 12 13"
                "                                          ....")
        self.assertEqual(ws_mod.parse_hex_dump(dump), bytes(range(20)))

    def test_garbage_returns_none(self):
        self.assertIsNone(ws_mod.parse_hex_dump("not a dump"))
        self.assertIsNone(ws_mod.parse_hex_dump(""))
        self.assertIsNone(ws_mod.parse_hex_dump("0000:   zz"))


class FlowInspectionTests(unittest.TestCase):
    def test_real_mitmweb_flow_is_websocket(self):
        self.assertTrue(ws_mod.is_websocket_flow(WS_FLOW))
        self.assertEqual(WS_FLOW["response"]["status_code"], 101)

    def test_plain_flow_is_not_websocket(self):
        flow = {"request": {"method": "GET", "path": "/api/x",
                            "headers": []},
                "response": {"status_code": 200}}
        self.assertFalse(ws_mod.is_websocket_flow(flow))

    def test_upgrade_header_plus_101_without_the_key(self):
        flow = {"request": {"method": "GET", "path": "/chat",
                            "headers": [["Upgrade", "websocket"]]},
                "response": {"status_code": 101}}
        self.assertTrue(ws_mod.is_websocket_flow(flow))

    def test_ws_url(self):
        flow = {"request": {"scheme": "https", "host": "example.com",
                            "port": 443, "path": "/chat"}}
        self.assertEqual(ws_mod.ws_url(flow), "wss://example.com/chat")
        flow["request"] = {"scheme": "http", "host": "127.0.0.1",
                           "port": 8080, "path": "/chat?room=1"}
        self.assertEqual(ws_mod.ws_url(flow),
                         "ws://127.0.0.1:8080/chat?room=1")

    def test_saved_messages_round_trip(self):
        flow = dict(WS_FLOW)
        flow["_saved_ws_messages"] = [
            {"from_client": True, "timestamp": 1.0, "text": "hi"},
            {"from_client": False, "timestamp": 1.1, "text": "ho"},
            {"from_client": True, "timestamp": 1.2,
             "b64": base64.b64encode(b"\x00\x01").decode()},
        ]
        self.assertEqual(len(ws_mod.ws_messages(flow)), 3)
        client = ws_mod.client_messages(flow)
        self.assertEqual(len(client), 2)
        self.assertEqual(ws_mod.message_bytes(client[0]),
                         (True, b"hi"))
        self.assertEqual(ws_mod.message_bytes(client[1]),
                         (False, b"\x00\x01"))

    def test_no_saved_messages_means_empty(self):
        self.assertEqual(ws_mod.ws_messages(WS_FLOW), [])


class ReplayTests(unittest.TestCase):
    def test_replay_ws_against_the_echo_server(self):
        with EchoServer() as server:
            flow = {
                "request": {"host": "127.0.0.1", "method": "GET",
                            "path": "/chat", "scheme": "http",
                            "port": server.port, "headers": []},
                "response": {"status_code": 101, "headers": []},
                "websocket": {"messages_meta": {"count": 2}},
                "_saved_ws_messages": [
                    {"from_client": True, "timestamp": 1.0,
                     "text": "hello-ws"},
                    {"from_client": False, "timestamp": 1.1,
                     "text": "hello-ws"},
                    {"from_client": True, "timestamp": 1.2,
                     "b64": base64.b64encode(b"\x00\x01").decode()},
                ],
            }
            result = ws_mod.replay_ws(flow, timeout=10)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["sent"], 2)
            self.assertEqual(result["received"],
                             [{"text": "hello-ws"},
                              {"b64": base64.b64encode(b"\x00\x01")
                               .decode()}])

    def test_replay_to_a_dead_server_is_a_result(self):
        flow = {
            "request": {"host": "127.0.0.1", "method": "GET",
                        "path": "/chat", "scheme": "http", "port": 9,
                        "headers": []},
            "response": {"status_code": 101, "headers": []},
            "websocket": {},
            "_saved_ws_messages": [
                {"from_client": True, "timestamp": 1.0, "text": "hi"}],
        }
        result = ws_mod.replay_ws(flow, timeout=5)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])


class MitmWsMessagesTests(unittest.TestCase):
    def test_ws_messages_from_the_real_fixtures(self):
        """The auto+raw pair from the live probe -> exact messages."""
        m = mitm.Mitm()
        auto_resp = mock.Mock(status_code=200)
        auto_resp.json.return_value = WS_AUTO
        raw_resp = mock.Mock(status_code=200)
        raw_resp.json.return_value = WS_RAW
        auth_resp = mock.Mock(status_code=200)

        def fake_get(url, **kw):
            if url.endswith("/"):
                return auth_resp
            if "/content/auto.json" in url:
                return auto_resp
            if "/content/raw.json" in url:
                return raw_resp
            raise AssertionError(f"unexpected URL: {url}")

        with mock.patch.object(m._http, "get", side_effect=fake_get):
            messages = m.ws_messages("flow-id")
        self.assertEqual(len(messages), 6)
        # Text messages: exact, from the raw view.
        self.assertEqual(messages[0],
                         {"from_client": True,
                          "timestamp": 1794591069.452963,
                          "text": "hello-ws"})
        self.assertEqual(messages[1]["from_client"], False)
        # Binary messages: exact bytes, parsed from the hex dump.
        self.assertEqual(messages[2]["from_client"], True)
        self.assertEqual(base64.b64decode(messages[2]["b64"]),
                         b"\x00\x01\x02bin")
        self.assertEqual(base64.b64decode(messages[4]["b64"]),
                         bytes(range(20)))

    def test_ws_messages_http_error_raises(self):
        m = mitm.Mitm()
        bad = mock.Mock(status_code=500)
        with mock.patch.object(m._http, "get", return_value=bad):
            with self.assertRaises(mitm.MitmError):
                m.ws_messages("flow-id")


if __name__ == "__main__":
    unittest.main()
