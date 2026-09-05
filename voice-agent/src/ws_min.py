"""ws_min — WebSocket (RFC 6455) server and client on the standard library.

Written for the voice plane and the agents (request 487): a phone streams
100 ms sealed audio frames over one socket per session, the plane relays
them, the agent answers on the same socket. None of the three has (or wants)
a WebSocket library — the plane is stdlib on purpose — and what a relay needs
is small: the handshake, frame framing with masking, fragmentation, ping/pong,
close. Nothing here interprets payloads.

Server side (inside a BaseHTTPRequestHandler's do_GET):
    ws = ws_min.accept(handler)          # 101 sent; handler.connection is now the socket
    op, data = ws.recv()                 # op is TEXT or BINARY; bytes either way
    ws.send_text("…"); ws.send_binary(b"…"); ws.close()

Client side:
    ws = ws_min.connect("wss://host/path", headers={"Authorization": "Bearer …"})

recv() raises ConnectionClosed when the peer closed or the socket died.
"""
import base64
import hashlib
import os
import socket
import ssl
import struct
import threading
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_FRAME = 64 * 1024 * 1024


class ConnectionClosed(Exception):
    pass


class WS:
    def __init__(self, sock, mask, timeout=None):
        self.sock = sock
        self.mask = mask                  # clients mask, servers do not
        self.closed = False
        self._rl = threading.Lock()
        self._wl = threading.Lock()
        # ALWAYS applied, None included. connect() opens the socket with a
        # 15 s timeout for the handshake, and a socket keeps its timeout until
        # told otherwise — so a relay leg that fell silent for 15 s "timed out"
        # and the stream ended (2026-09-05, 19:48 and 19:51 UTC, twice on the
        # owner's phone). None means blocking, which is what a long-lived
        # socket wants; callers that need a bound pass it to recv().
        sock.settimeout(timeout)

    # ---- reading ---------------------------------------------------------
    def _read_exact(self, n):
        chunks, got = [], 0
        while got < n:
            try:
                b = self.sock.recv(min(n - got, 1 << 16))
            except (ConnectionError, OSError) as e:
                raise ConnectionClosed(str(e))
            if not b:
                raise ConnectionClosed("socket closed")
            chunks.append(b)
            got += len(b)
        return b"".join(chunks)

    def _read_frame(self):
        h = self._read_exact(2)
        fin = bool(h[0] & 0x80)
        op = h[0] & 0x0F
        masked = bool(h[1] & 0x80)
        n = h[1] & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8))[0]
        if n > MAX_FRAME:
            self.close(1009)
            raise ConnectionClosed(f"frame of {n} bytes refused")
        key = self._read_exact(4) if masked else None
        data = self._read_exact(n) if n else b""
        if key:
            data = _xor(data, key)
        return fin, op, data

    def recv(self, timeout=None):
        """(TEXT|BINARY, bytes) for the next data message; pings answered here."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        with self._rl:
            buf, msg_op = [], None
            while True:
                fin, op, data = self._read_frame()
                if op == PING:
                    self._send_raw(PONG, data)
                    continue
                if op == PONG:
                    continue
                if op == CLOSE:
                    try:
                        self._send_raw(CLOSE, data[:2])
                    except Exception:
                        pass
                    self.closed = True
                    raise ConnectionClosed("peer closed")
                if op in (TEXT, BINARY):
                    if msg_op is not None:
                        raise ConnectionClosed("new message inside a fragment")
                    msg_op = op
                elif op == CONT:
                    if msg_op is None:
                        raise ConnectionClosed("continuation without a start")
                else:
                    raise ConnectionClosed(f"unknown opcode {op}")
                buf.append(data)
                if fin:
                    return msg_op, b"".join(buf)

    # ---- writing ---------------------------------------------------------
    def _send_raw(self, op, data):
        n = len(data)
        head = bytes([0x80 | op])
        if n < 126:
            head += bytes([(0x80 if self.mask else 0) | n])
        elif n < 65536:
            head += bytes([(0x80 if self.mask else 0) | 126]) + struct.pack("!H", n)
        else:
            head += bytes([(0x80 if self.mask else 0) | 127]) + struct.pack("!Q", n)
        if self.mask:
            key = os.urandom(4)
            head += key
            data = _xor(data, key)
        with self._wl:
            try:
                self.sock.sendall(head + data)
            except (ConnectionError, OSError) as e:
                self.closed = True
                raise ConnectionClosed(str(e))

    def send_text(self, s):
        self._send_raw(TEXT, s.encode("utf-8") if isinstance(s, str) else s)

    def send_binary(self, b):
        self._send_raw(BINARY, bytes(b))

    def ping(self, data=b""):
        self._send_raw(PING, data)

    def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        try:
            self._send_raw(CLOSE, struct.pack("!H", code))
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _xor(data, key):
    if not data:
        return data
    k = (key * (len(data) // 4 + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, k))


def is_upgrade(headers):
    return ("websocket" in (headers.get("Upgrade") or "").lower()
            and "upgrade" in (headers.get("Connection") or "").lower()
            and bool(headers.get("Sec-WebSocket-Key")))


def accept(handler, timeout=None):
    """Complete the handshake on a BaseHTTPRequestHandler; returns a WS."""
    key = handler.headers.get("Sec-WebSocket-Key") or ""
    if not is_upgrade(handler.headers):
        raise ValueError("not a websocket upgrade")
    acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    handler.wfile.write(
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        + f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
    handler.wfile.flush()
    handler.close_connection = True
    return WS(handler.connection, mask=False, timeout=timeout)


def connect(url, headers=None, timeout=10):
    """Open a client socket to ws:// or wss:// url; returns a WS (masked)."""
    u = urllib.parse.urlparse(url)
    tls = u.scheme in ("wss", "https")
    port = u.port or (443 if tls else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    req = [f"GET {path} HTTP/1.1", f"Host: {u.hostname}:{port}" if u.port else f"Host: {u.hostname}",
           "Upgrade: websocket", "Connection: Upgrade",
           f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    for k, v in (headers or {}).items():
        req.append(f"{k}: {v}")
    sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        b = sock.recv(4096)
        if not b:
            raise ConnectionClosed("handshake: socket closed")
        resp += b
        if len(resp) > 65536:
            raise ConnectionClosed("handshake: response too long")
    head, _, rest = resp.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 101" not in status:
        raise ConnectionClosed(f"handshake refused: {status[:80]}")
    want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    hdrs = {l.split(b":", 1)[0].strip().lower(): l.split(b":", 1)[1].strip()
            for l in head.split(b"\r\n")[1:] if b":" in l}
    if hdrs.get(b"sec-websocket-accept", b"").decode() != want:
        raise ConnectionClosed("handshake: bad accept key")
    ws = WS(sock, mask=True, timeout=None)
    if rest:
        # bytes that arrived with the handshake belong to the first frame
        ws._pending = rest
        ws._read_exact = _prefixed_reader(ws, rest)
    return ws


def _prefixed_reader(ws, rest):
    orig = WS._read_exact.__get__(ws, WS)
    state = {"buf": rest}

    def read(n):
        out = b""
        if state["buf"]:
            take, state["buf"] = state["buf"][:n], state["buf"][n:]
            out = take
        if len(out) < n:
            out += orig(n - len(out))
        return out
    return read
