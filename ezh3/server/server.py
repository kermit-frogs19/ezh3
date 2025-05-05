import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional, Callable
import ssl
from pathlib import Path
from ezh3.server.certificate import generate_self_signed_cert
from typing import Literal

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.asyncio.server import QuicServer
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived, DatagramReceived, DataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import stream_is_unidirectional
from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent

from ezh3.server.server_request import ServerRequest
from ezh3.server.responses import Response, JSONResponse, TextResponse
from ezh3.server.route_handler import RouteHandler
from ezh3.common.config import AllowedMethods, ALLOWED_METHODS
from ezh3.server.server_connection import ServerConnection

logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.DEBUG,
    )

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)


class Server:
    def __init__(
            self,
            title: str = "",
            host: str = "127.0.0.1",
            port: int = 443,
            enable_tls: bool = False,
            custom_cert_file_loc: str = None,
            custom_cert_key_file_loc: str = None,
            cert_type: Literal["SELF_SIGNED", "CUSTOM", None] = "SELF_SIGNED"
    ):
        self.title = title
        self.host = host
        self.port = port
        self.enable_tls = enable_tls
        self.custom_cert_file_loc = custom_cert_file_loc
        self.custom_cert_key_file_loc = custom_cert_key_file_loc
        self.cert_type = cert_type

        # Route registry, a key value pair of a tuple(path, method) to a RequestHandler instance
        self.routes: dict[tuple[str, str], RouteHandler] = {}
        self.cert_file_loc: str | None = None
        self.cert_key_file_loc: str | None = None
        self.configuration: QuicConfiguration | None = None
        self.server: QuicServer | None = None
        self._is_running: bool = False
        self.connections: set[ServerConnection] = set()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def _configure(self):
        self.configuration = QuicConfiguration(is_client=False, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536)

        if self.cert_type == "SELF_SIGNED":
            self.cert_file_loc = "cert.pem"
            self.cert_key_file_loc = "key.pem"

            # Auto-generate certificate if missing
            generate_self_signed_cert(self.cert_file_loc, self.cert_key_file_loc)

        if self.enable_tls:
            if not self.custom_cert_file_loc:
                raise ValueError("Parameter that holds custom certificate file location - custom_cert_file_loc not provided")

            if not self.custom_cert_key_file_loc:
                raise ValueError("Parameter that holds custom certificate key file location - custom_cert_key_file_loc not provided")

            self.cert_file_loc = self.custom_cert_file_loc
            self.cert_key_file_loc = self.custom_cert_key_file_loc
            self.configuration.verify_mode = ssl.CERT_REQUIRED
        else:
            self.configuration.verify_mode = ssl.CERT_NONE  # **Disable TLS in QUIC**

        self.configuration.load_cert_chain(Path(self.cert_file_loc), Path(self.cert_key_file_loc))

    def route(self, path: str, method: str = "GET") -> Callable:
        """Decorator for registering a route"""
        def decorator(func):
            self.routes[path, method] = RouteHandler(method=method, function=func)
            return func
        return decorator

    def get(self, path) -> Callable:
        return self.route(path, "GET")

    def post(self, path) -> Callable:
        return self.route(path, "POST")

    def patch(self, path) -> Callable:
        return self.route(path, "PATCH")

    def put(self, path) -> Callable:
        return self.route(path, "PUT")

    def delete(self, path) -> Callable:
        return self.route(path, "DELETE")

    async def handle_request(self, request: ServerRequest) -> Response:
        """Processes request and returns response"""
        handler = self.routes.get((request.path, request.method), None)
        if not handler:
            other_handler = next((self.routes[(request.path, method)] for method in ALLOWED_METHODS if (request.path, method) in self.routes), None)
            if other_handler:
                return JSONResponse(status_code=405, content={"error": f"Method {request.method} not allowed for path {request.path}"})
            return JSONResponse(status_code=404, content={"error": f"Not Found path {request.path}"})

        kwargs = {}
        for param_name, param in handler.parameters.items():
            type_ = param.annotation
            if type_ == ServerRequest:
                kwargs[param_name] = request

        result = None
        try:
            result = await handler.function(**kwargs) if asyncio.iscoroutinefunction(handler.function) else (
                handler.function(**kwargs))
            if isinstance(result, Response):
                return result
            if isinstance(result, (list, dict)):
                return JSONResponse(status_code=200, content=result)
            if isinstance(result, bytes):
                return Response(status_code=200, content=result)

            return TextResponse(status_code=200, content=str(result))

        except BaseException as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"Internal server error. Unsupported function request handler return type: {type(result).__name__}"}
            )

    def shutdown(self):
        if not self.is_running:
            return

        for connection in list(self.connections):
            connection.cleanup()

        self.connections.clear()
        self.server.close()
        self._is_running = False

    async def run(self, host: str = "0.0.0.0", port: int = 443) -> None:
        """Starts QUIC server"""
        self.host = host  # Force override
        self.port = port

        self._configure()

        self.server = await serve(
            host=self.host,
            port=self.port,
            configuration=self.configuration,
            create_protocol=lambda *args, **kwargs: self._track_connections(ServerConnection(self, *args, **kwargs)),
        )
        self._is_running = True

        print(f"QUIC HTTP/3 Server running on {self.host}:{self.port}")
        try:
            logger.info("Listening on https://{}:{}".format(self.host, self.port))
            await asyncio.Future()  # ✅ Blocks the event loop forever
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.shutdown()
            print("[INFO] QUIC server shutting down...")

    def _track_connections(self, connection: ServerConnection) -> ServerConnection:
        self.connections.add(connection)
        connection.on_close = lambda: self.connections.discard(connection)
        return connection


