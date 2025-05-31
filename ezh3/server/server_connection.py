import asyncio
from typing import Optional, Callable

from aioquic.asyncio import QuicConnectionProtocol
from aioquic.h3.connection import H3Connection, ErrorCode
from aioquic.h3.events import H3Event, HeadersReceived, DataReceived, WebTransportStreamDataReceived
from collections import defaultdict

from aioquic.quic.events import ProtocolNegotiated, StreamReset, QuicEvent, ConnectionTerminated

from ezh3.server.server_request import ServerRequest
from ezh3.server.responses import *


class ServerConnection(QuicConnectionProtocol):

    def __init__(self, server, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.server = server
        self._http: Optional[H3Connection] = None
        self._requests: dict[int, ServerRequest] = {}  # ✅ Stores request data (headers + body)
        self._stream_queues: dict[int, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._stream_tasks: dict[int, asyncio.Task] = {}

        self.on_close: Callable = lambda: None

    def quic_event_received(self, event: QuicEvent) -> None:
        if isinstance(event, ProtocolNegotiated):
            if event.alpn_protocol == "h3":
                self._http = H3Connection(self._quic, enable_webtransport=True)

        elif isinstance(event, ConnectionTerminated):
            # Cleanup everything
            self.cleanup()

        if self._http is not None:
            for h3_event in self._http.handle_event(event):
                stream_id = h3_event.stream_id
                queue = self._stream_queues[stream_id]
                queue.put_nowait(h3_event)

                # Start one task per stream if not already running
                if stream_id not in self._stream_tasks:
                    self._stream_tasks[stream_id] = asyncio.create_task(
                        self._stream_worker(stream_id, queue)
                    )

                # asyncio.create_task(self._h3_event_received(h3_event))

    async def _stream_worker(self, stream_id: int, queue: asyncio.Queue):
        try:
            while True:
                event = await queue.get()
                await self._h3_event_received(event)

                if event.__class__ in {DataReceived, HeadersReceived} and event.stream_ended is True:
                    break

        finally:
            # Cleanup once stream is fully handled
            self._stream_queues.pop(stream_id, None)
            # noinspection PyAsyncCall
            self._stream_tasks.pop(stream_id, None)

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

        self.transmit()

    def close_stream(self, stream_id: int):
        # Sending
        self._quic.reset_stream(stream_id=stream_id, error_code=ErrorCode.H3_REQUEST_CANCELLED)
        self.transmit()

    def cleanup(self) -> None:
        self._requests.clear()
        self.on_close()
