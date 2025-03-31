  # ref: https://github.com/GoogleChrome/samples/tree/gh-pages/webtransport

import argparse
import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional
import ssl

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived, DatagramReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import stream_is_unidirectional
from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent
from pathlib import Path
from server.certificate import generate_self_signed_cert
from typing import Literal


BIND_ADDRESS = "127.0.0.1"
BIND_PORT = 29024

logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG,
    )

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)




class CounterHandler:

    def __init__(self, session_id, http: H3Connection) -> None:
        self._session_id = session_id
        self._http = http
        self._counters = defaultdict(int)

    def h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, DatagramReceived):
            payload = str(len(event.data)).encode('ascii')
            self._http.send_datagram(self._session_id, payload)

        if isinstance(event, WebTransportStreamDataReceived):
            self._counters[event.stream_id] += len(event.data)
            if event.stream_ended:
                if stream_is_unidirectional(event.stream_id):
                    response_id = self._http.create_webtransport_stream(
                        self._session_id, is_unidirectional=True)
                else:
                    response_id = event.stream_id
                payload = str(self._counters[event.stream_id]).encode('ascii')
                self._http._quic.send_stream_data(
                    response_id, payload, end_stream=True)
                self.stream_closed(event.stream_id)

    def stream_closed(self, stream_id: int) -> None:
        try:
            del self._counters[stream_id]
        except KeyError:
            pass


class WebTransportProtocol(QuicConnectionProtocol):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http: Optional[H3Connection] = None

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated):
            if event.alpn_protocol == "h3":
                self._http = H3Connection(self._quic, enable_webtransport=True)
                print("ProtocolNegotiated")
        elif isinstance(event, StreamReset):
            print(event)

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                self._h3_event_received(h3_event)

    def _h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            headers = {header: value for header, value in event.headers}
            method = headers.get(b":method", b"").decode()
            path = headers.get(b":path", b"").decode()

            print(f"Received HTTP Request: {method} {path}")

            if method == "GET":
                self._handle_http_get(event.stream_id, path)
            elif method == "POST":
                self._handle_http_post(event.stream_id, headers)
            elif (headers.get(b":method") == b"CONNECT" and
                    headers.get(b":protocol") == b"webtransport"):
                self._handshake_webtransport(event.stream_id, headers)
            else:
                self._send_response(event.stream_id, 400, end_stream=True)

    def _handshake_webtransport(self, stream_id: int, request_headers: Dict[bytes, bytes]) -> None:
        authority = request_headers.get(b":authority")
        path = request_headers.get(b":path")
        if authority is None or path is None:
            self._send_response(stream_id, 400, end_stream=True)
            return
        self._send_response(stream_id, 200)

    def _handle_http_get(self, stream_id: int, path: str):
        """Handles HTTP GET requests"""
        if path == "/":
            body = b"Hello, this is an HTTP/3 response!"
            self._send_response(stream_id, 200)
            self._http.send_data(stream_id, body, end_stream=True)
        else:
            self._send_response(stream_id, 404, end_stream=True)

    def _handle_http_post(self, stream_id: int, headers: Dict[bytes, bytes]):
        """Handles HTTP POST requests (simple text response)"""
        body = b"POST request received!"
        self._send_response(stream_id, 200)
        self._http.send_data(stream_id, body, end_stream=True)

    def _send_response(self, stream_id: int, status_code: int, end_stream=False) -> None:
        headers = [(b":status", str(status_code).encode())]
        if status_code == 200:
            headers.append((b"sec-webtransport-http3-draft", b"draft02"))
        self._http.send_headers(stream_id=stream_id, headers=headers, end_stream=end_stream)


class Server:
    def __init__(
            self,
            host: str = "127.0.0.1",
            port: int = 443,
            enable_tls: bool = False,
            custom_cert_file_loc: str = None,
            custom_cert_key_file_loc: str = None,
            cert_type: Literal["SELF_SIGNED", "CUSTOM", None] = "SELF_SIGNED"
    ):
        self.host = host
        self.port = port
        self.enable_tls = enable_tls
        self.cert_file_loc = None
        self.cert_key_file_loc = None
        self.routes = {}  # Route registry
        self.cert_type = cert_type

        self.configuration = QuicConfiguration(is_client=False)

        if self.cert_type == "SELF_SIGNED":
            self.cert_file_loc = "cert.pem"
            self.cert_key_file_loc = "key.pem"

            # Auto-generate certificate if missing
            generate_self_signed_cert(self.cert_file_loc, self.cert_key_file_loc)

        if self.enable_tls:
            self.cert_file_loc = custom_cert_file_loc
            self.cert_key_file_loc = custom_cert_key_file_loc
            self.configuration.load_cert_chain(Path(self.cert_file_loc), Path(self.cert_key_file_loc))
        else:
            self.configuration.verify_mode = ssl.CERT_NONE  # **Disable TLS in QUIC**

    def route(self, path: str, method: str):
        """Decorator for registering a route"""
        def decorator(func):
            self.routes[path] = {"func": func, "method": method}
            return func
        return decorator

    def get(self, path):
        return self.route(path, "GET")

    def post(self, path):
        return self.route(path, "POST")

    async def handle_request(self, path, data, method):
        """Processes request and returns response"""
        handler = self.routes.get(path, None)
        if not handler:
            return {"error": "Route not found"}

        if handler["method"] != method:
            return {"error": "Method not allowed"}

        return await handler["func"](data) if asyncio.iscoroutinefunction(handler) else handler["func"](data)

    async def run(self):
        """Starts QUIC server"""
        await serve(
            host=self.host,
            port=self.port,
            configuration=self.configuration,
            create_protocol=lambda *args, **kwargs: WebTransportProtocol(self.handle_request, *args, **kwargs),
        )
        print(f"QUIC HTTP/3 Server running on {self.host}:{self.port}")
        try:
            logger.info("Listening on https://{}:{}".format(BIND_ADDRESS, BIND_PORT))
            await asyncio.Future()  # ✅ Blocks the event loop forever
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("[INFO] QUIC server shutting down...")


async def main():
    configuration = QuicConfiguration(
        alpn_protocols=H3_ALPN,
        is_client=False,
        max_datagram_frame_size=65536,
    )
    # Auto-generate certificate if missing
    generate_self_signed_cert("cert.pem", "key.pem")

    configuration.load_cert_chain(Path("cert.pem"), Path("key.pem"))
    configuration.verify_mode = ssl.CERT_NONE

    await serve(
            BIND_ADDRESS,
            BIND_PORT,
            configuration=configuration,
            create_protocol=WebTransportProtocol,
        )
    try:
        logger.info("Listening on https://{}:{}".format(BIND_ADDRESS, BIND_PORT))
        await asyncio.Future()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    asyncio.run(main())
