"""Drone-side persistent outbound connection to the Overmind Edge service.

The Drone opens **one** long-lived TLS connection to the Edge and keeps it warm.
This is what lets a Drone work behind a normal home router with no port-forward:
all traffic is Drone-initiated/outbound, and the Edge pushes presence/signaling
(and, later, relayed data) back down this same connection.

Design split for testability:

* :class:`MuxSession` is the pure, synchronous protocol core -- feed it decoded
  frames, it returns frames to send and updates its state. No threads, no I/O.
* :class:`MuxClient` is the thin threaded runner: it connects a :class:`MuxLink`,
  drives a :class:`MuxSession`, replies to keepalives, and reconnects with capped
  backoff. A reader thread does blocking reads; only the main loop writes, so no
  send lock is needed.

The link is abstracted (:class:`MuxLink`) so the persistent transport can evolve
(raw TLS today via :class:`TlsMuxLink`; a WebSocket framing could slot in later)
without touching the protocol core.
"""

from __future__ import annotations

import queue
import socket
import ssl
import threading
import time
from typing import Any, Callable, List, Optional, Protocol
from urllib.parse import urlsplit

from . import mux


class MuxLink(Protocol):
    """A connected duplex byte channel to the Edge."""

    def send(self, data: bytes) -> None: ...

    def read_exactly(self, n: int) -> bytes: ...

    def close(self) -> None: ...


class MuxSession:
    """Pure protocol state machine for one Edge connection (no I/O)."""

    def __init__(
        self,
        *,
        device_id: str,
        token: str,
        capabilities: Optional[List[str]] = None,
        lan_addrs: Optional[List[str]] = None,
        app_version: Optional[str] = None,
        on_presence: Optional[Callable[[list], None]] = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.device_id = device_id
        self.token = token
        self.capabilities = list(capabilities or [])
        self.lan_addrs = list(lan_addrs or [])
        self.app_version = app_version
        self._on_presence = on_presence
        self._now = now
        self.session_id: Optional[str] = None
        self.reflexive_addr: Optional[str] = None
        self.connected = False
        self.last_activity = now()

    def build_hello(self) -> bytes:
        message = {
            "type": mux.MSG_HELLO,
            "device_id": self.device_id,
            "token": self.token,
            "capabilities": self.capabilities,
        }
        if self.lan_addrs:
            message["lan_addrs"] = self.lan_addrs
        if self.app_version:
            message["app_version"] = self.app_version
        return mux.encode_control(message)

    def build_ping(self) -> bytes:
        return mux.encode_control({"type": mux.MSG_PING, "t": self._now()})

    def handle_frame(self, kind: int, payload: bytes) -> List[bytes]:
        """Process one received frame; return any frames to send in response."""
        self.last_activity = self._now()
        if kind != mux.FRAME_CONTROL:
            # Phase 1 ignores DATA frames; the relay transport (Phase 2) handles them.
            return []
        message = mux.decode_control(payload)
        message_type = message.get("type")
        if message_type == mux.MSG_HELLO_ACK:
            self.session_id = message.get("session_id")
            self.reflexive_addr = message.get("reflexive_addr")
            self.connected = True
            return []
        if message_type == mux.MSG_PING:
            return [mux.encode_control({"type": mux.MSG_PONG, "t": message.get("t")})]
        if message_type == mux.MSG_PONG:
            return []
        if message_type == mux.MSG_PRESENCE:
            if self._on_presence is not None:
                self._on_presence(message.get("swarm") or message.get("peers") or [])
            return []
        if message_type in (mux.MSG_BYE, mux.MSG_ERROR):
            self.connected = False
        return []


def parse_edge_endpoint(edge_url: str, default_port: int = 443) -> tuple[str, int]:
    """Parse an Edge endpoint into ``(host, port)``.

    Accepts ``tls://host:port``, ``wss://host:port``, ``https://host:port`` or a
    bare ``host:port`` / ``host``. The scheme only signals "this is a TLS
    endpoint"; the mux framing rides directly on the TLS stream.
    """
    raw = str(edge_url or "").strip()
    if "://" not in raw:
        raw = "tls://" + raw
    parts = urlsplit(raw)
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"invalid edge url: {edge_url!r}")
    return host, int(parts.port or default_port)


class TlsMuxLink:
    """A :class:`MuxLink` backed by a TLS socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        # Buffered reader; mux framing reads exact byte counts off this.
        self._fileobj = sock.makefile("rb")
        self._read_exactly = mux.reader_from_fileobj(self._fileobj)

    def send(self, data: bytes) -> None:
        self._sock.sendall(data)

    def read_exactly(self, n: int) -> bytes:
        return self._read_exactly(n)

    def close(self) -> None:
        try:
            self._fileobj.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


def connect_tls(
    edge_url: str,
    *,
    verify: bool = True,
    cafile: Optional[str] = None,
    client_cert: Optional[str] = None,
    client_key: Optional[str] = None,
    timeout: float = 15.0,
    default_port: int = 443,
) -> TlsMuxLink:
    """Open a TLS connection to the Edge and return a :class:`TlsMuxLink`."""
    host, port = parse_edge_endpoint(edge_url, default_port=default_port)
    if verify:
        context = ssl.create_default_context(cafile=cafile)
    else:
        context = ssl._create_unverified_context()  # self-signed Edge / local dev
    if client_cert and client_key:
        try:
            context.load_cert_chain(certfile=client_cert, keyfile=client_key)
        except (ssl.SSLError, OSError):
            # mTLS is best-effort here; HELLO bearer-token auth is authoritative.
            pass
    raw_sock = socket.create_connection((host, port), timeout=timeout)
    try:
        tls_sock = context.wrap_socket(raw_sock, server_hostname=host if verify else None)
    except Exception:
        raw_sock.close()
        raise
    # Clear the connect timeout; blocking reads are governed by the read loop.
    tls_sock.settimeout(None)
    return TlsMuxLink(tls_sock)


class MuxClient:
    """Maintains a single persistent Edge connection in a background thread."""

    def __init__(
        self,
        *,
        connect: Callable[[], MuxLink],
        session_factory: Callable[[], MuxSession],
        ping_interval: float = 20.0,
        idle_timeout_multiplier: float = 3.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] = lambda message: None,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        self._connect = connect
        self._session_factory = session_factory
        self._ping_interval = max(1.0, float(ping_interval))
        self._idle_timeout = self._ping_interval * max(2.0, float(idle_timeout_multiplier))
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._sleep = sleep
        self._now = now
        self._log = log
        self._stop = stop_event or threading.Event()
        #: Latest session, exposed for status/presence inspection.
        self.session: Optional[MuxSession] = None

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        backoff = self._backoff_initial
        while not self._stop.is_set():
            try:
                connected = self._run_once()
            except Exception as error:  # noqa: BLE001 -- never let the loop die
                self._log(f"edge mux connection error: {error}")
                connected = False
            if self._stop.is_set():
                break
            # Reset backoff after a session that actually came up.
            backoff = self._backoff_initial if connected else min(backoff * 2, self._backoff_max)
            self._sleep(backoff)

    def _run_once(self) -> bool:
        link = self._connect()
        session = self._session_factory()
        self.session = session
        inbound: "queue.Queue[tuple]" = queue.Queue()
        reader = threading.Thread(
            target=self._read_loop, args=(link, inbound), name="edge-mux-reader", daemon=True
        )
        reader.start()
        try:
            link.send(session.build_hello())
            while not self._stop.is_set():
                try:
                    item = inbound.get(timeout=self._ping_interval)
                except queue.Empty:
                    if self._now() - session.last_activity > self._idle_timeout:
                        self._log("edge mux idle timeout; reconnecting")
                        break
                    link.send(session.build_ping())
                    continue
                if item[0] == "error":
                    self._log(f"edge mux read ended: {item[1]}")
                    break
                _, kind, payload = item
                for frame in session.handle_frame(kind, payload):
                    link.send(frame)
        finally:
            link.close()
            reader.join(timeout=2.0)
        return session.connected

    @staticmethod
    def _read_loop(link: MuxLink, inbound: "queue.Queue[tuple]") -> None:
        try:
            while True:
                kind, payload = mux.read_frame(link.read_exactly)
                inbound.put(("msg", kind, payload))
        except Exception as error:  # noqa: BLE001 -- propagate as a queue item
            inbound.put(("error", error))
