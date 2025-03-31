import asyncio
import logging
from collections import defaultdict
from typing import Dict, Optional, Callable
import ssl
from pathlib import Path
from server.certificate import generate_self_signed_cert
from typing import Literal

from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.h3.connection import H3_ALPN, H3Connection
from aioquic.h3.events import H3Event, HeadersReceived, WebTransportStreamDataReceived, DatagramReceived, DataReceived
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import stream_is_unidirectional
from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent

from ezh3.client.server_request import ServerRequest
from ezh3.client.response import Response, JSONResponse, TextResponse
from ezh3.client.request_handler import RequestHandler
from ezh3.client.config import AllowedMethods, ALLOWED_METHODS


BIND_ADDRESS = "0.0.0.0"
BIND_PORT = 443

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
        self.routes: dict[tuple[str, str], RequestHandler] = {}
        self.cert_file_loc: str | None = None
        self.cert_key_file_loc: str | None = None
        self.configuration: QuicConfiguration | None = None
        self.server: HTTPProtocol | None = None
        self._is_running: bool = False

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
                raise ValueError("Parameter that holds custom certificate file location - custom_cert_file_loc not provided ")

            if not self.custom_cert_key_file_loc:
                raise ValueError("Parameter that holds custom certificate key file location - custom_cert_key_file_loc not provided ")

            self.cert_file_loc = self.custom_cert_file_loc
            self.cert_key_file_loc = self.custom_cert_key_file_loc
            self.configuration.verify_mode = ssl.CERT_REQUIRED
        else:
            self.configuration.verify_mode = ssl.CERT_NONE  # **Disable TLS in QUIC**

        self.configuration.load_cert_chain(Path(self.cert_file_loc), Path(self.cert_key_file_loc))

    def route(self, path: str, method: str = "GET") -> Callable:
        """Decorator for registering a route"""
        def decorator(func):
            self.routes[path, method] = RequestHandler(method=method, function=func)
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
        self.server.close()
        self._is_running = False

    async def run(self, host: str = "0.0.0.0", port: int = 443) -> None:
        """Starts QUIC server"""
        if host not in {self.host, "0.0.0.0"}:
            self.host = host
        if port not in {self.port, 443}:
            self.port = port

        self._configure()

        self.server = await serve(
            host=self.host,
            port=self.port,
            configuration=self.configuration,
            create_protocol=lambda *args, **kwargs: HTTPProtocol(self, *args, **kwargs),
        )
        self._is_running = True

        print(f"QUIC HTTP/3 Server running on {self.host}:{self.port}")
        try:
            logger.info("Listening on https://{}:{}".format(BIND_ADDRESS, BIND_PORT))
            await asyncio.Future()  # ✅ Blocks the event loop forever
        except (asyncio.CancelledError, KeyboardInterrupt):
            self.shutdown()
            print("[INFO] QUIC server shutting down...")


class HTTPProtocol(QuicConnectionProtocol):

    def __init__(self, server: Server, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.server = server
        self._http: Optional[H3Connection] = None
        self._requests: dict[int, ServerRequest] = {}  # ✅ Stores request data (headers + body)

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated):
            if event.alpn_protocol == "h3":
                self._http = H3Connection(self._quic, enable_webtransport=True)

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                asyncio.create_task(self._h3_event_received(h3_event))

    async def _h3_event_received(self, event: H3Event) -> None:
        if isinstance(event, HeadersReceived):
            self._requests[event.stream_id] = ServerRequest(
                raw_headers=event.headers,
            )

        elif isinstance(event, DataReceived):
            if event.stream_id in self._requests:
                self._requests[event.stream_id].body += event.data

        elif isinstance(event, StreamReset):
            if event.stream_id in self._requests:
                del self._requests[event.stream_id]

        if event.stream_ended:
            await self._process_request(event.stream_id)

    async def _process_request(self, stream_id: int) -> None:
        """Processes a fully received HTTP request."""
        if stream_id not in self._requests:
            return

        request = self._requests.pop(stream_id)  # ✅ Remove request from storage
        response = await self.server.handle_request(request)

        self.send_response(stream_id=stream_id, response=response)

    def send_response(self, stream_id: int, response: Response) -> None:
        body = response.render_body()
        headers = response.render_headers()

        self._http.send_headers(stream_id=stream_id, headers=headers, end_stream=False)
        self._http.send_data(stream_id=stream_id, data=body, end_stream=True)


app = Server(
    enable_tls=True,
    cert_type="CUSTOM",
    custom_cert_file_loc="/etc/letsencrypt/live/vadim-seliukov-quic-server.com/fullchain.pem",
    custom_cert_key_file_loc="/etc/letsencrypt/live/vadim-seliukov-quic-server.com/privkey.pem"
)


@app.get("/")
async def home():
    return {"message": "Welcome to QUIC Server"}


@app.post("/echo")
async def echo(request: ServerRequest):
    data = request.json()
    return data


async def main1():
    await app.run(port=BIND_PORT, host=BIND_ADDRESS)


if __name__ == '__main__':
    asyncio.run(main1())
