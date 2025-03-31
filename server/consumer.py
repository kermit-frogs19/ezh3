import asyncio
import msgpack
import ssl
from aioquic.asyncio import QuicConnectionProtocol, serve
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import StreamDataReceived, HandshakeCompleted
from pathlib import Path
from server.certificate import generate_self_signed_cert
from typing import Literal


class QUICFastAPI:
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

        if self.enable_tls:
            self.cert_file_loc = custom_cert_file_loc
            self.cert_key_file_loc = custom_cert_key_file_loc

            if self.cert_type == "SELF_SIGNED":
                self.cert_file_loc = "cert.pem"
                self.cert_key_file_loc = "key.pem"

                # Auto-generate certificate if missing
                generate_self_signed_cert(self.cert_file_loc, self.cert_key_file_loc)

            self.configuration.load_cert_chain(Path(self.cert_file_loc), Path(self.cert_key_file_loc))
        else:
            self.configuration.verify_mode = ssl.CERT_NONE  # **Disable TLS in QUIC**

    def route(self, path):
        """Decorator for registering a route"""
        def decorator(func):
            self.routes[path] = func
            return func
        return decorator

    async def handle_request(self, path, data):
        """Processes request and returns response"""
        handler = self.routes.get(path, None)
        if handler:
            return await handler(data) if asyncio.iscoroutinefunction(handler) else handler(data)
        return {"error": "Route not found"}

    async def run(self):
        """Starts QUIC server"""
        await serve(
            host=self.host,
            port=self.port,
            configuration=self.configuration,
            create_protocol=lambda *args, **kwargs: QUICServerProtocol(self.handle_request, *args, **kwargs),
        )
        print(f"QUIC HTTP/3 Server running on {self.host}:{self.port}")
        try:
            await asyncio.Future()  # ✅ Blocks the event loop forever
        except (asyncio.CancelledError, KeyboardInterrupt):
            print("[INFO] QUIC server shutting down...")


class QUICServerProtocol(QuicConnectionProtocol):
    """Handles QUIC requests and responses"""
    def __init__(self, handler, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.handler = handler

    async def quic_event_received(self, event):
        if isinstance(event, HandshakeCompleted):
            print(f"New QUIC connection established")

        elif isinstance(event, StreamDataReceived):
            stream_id = event.stream_id
            try:
                # Unpack data from MessagePack
                print(f"Received data: {event.data}")

                request = msgpack.unpackb(event.data, raw=False)

                print(f"Received request: {request}")

                # Extract path and data
                path = request.get("path", "/")
                data = request.get("data", {})

                # Process request
                response = await self.handler(path, data)

                # Send MessagePack response
                # packed_response = msgpack.packb(response, use_bin_type=True)
                self._quic.send_stream_data(stream_id, response, end_stream=True)
                self.transmit()
            except Exception as e:
                print(f"Error handling request: {e}")


# ----------------- SERVER USAGE -----------------
app = QUICFastAPI(port=1234, host="0.0.0.0")


@app.route("/")
def home(data):
    return {"message": "Welcome to QUICFastAPI"}


# Start the server
if __name__ == "__main__":
    asyncio.run(app.run())