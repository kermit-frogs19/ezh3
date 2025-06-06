import asyncio
from collections import deque
from typing import Deque

from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.connection import END_STATES
from aioquic.h3.connection import H3_ALPN, ErrorCode, H3Connection
from aioquic.quic.events import QuicEvent
from aioquic.h3.events import DataReceived, H3Event, HeadersReceived, PushPromiseReceived

from ezh3.client.client_websocket import ClientWebSocket


class ClientConnection(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.pushes: dict[int, Deque[H3Event]] = {}
        self._http: H3Connection | None = None
        self._request_events: dict[int, Deque[H3Event]] = {}
        self._request_waiter: dict[int, asyncio.Future[Deque[H3Event]]] = {}
        self._websockets: dict[int, ClientWebSocket] = {}
        self._http = H3Connection(self._quic)

        self.port: int | None = None
        self.host: str | None = None
        self.transport = None

    def __hash__(self) -> int:
        """Defines hash based on host and port to allow using a set."""
        return hash((self.host, self.port))

    def __eq__(self, other) -> bool:
        """Ensures that two objects with the same host/port are considered equal."""
        if not isinstance(other, ClientConnection):
            return False
        return self.host == other.host and self.port == other.port

    @property
    def is_running(self) -> bool:
        return self._quic._close_event is None and self._quic._state not in END_STATES

    async def aclose(self, error_code=None):
        if not self.is_running:
            return

        error_code = ErrorCode.H3_NO_ERROR if error_code is None else error_code
        self.close(error_code=error_code)
        await self.wait_closed()
        self.transport.close()

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, (HeadersReceived, DataReceived)):
            stream_id = event.stream_id
            if stream_id in self._request_events:
                # http
                self._request_events[event.stream_id].append(event)
                if event.stream_ended:
                    request_waiter = self._request_waiter.pop(stream_id)
                    request_waiter.set_result(self._request_events.pop(stream_id))

            elif stream_id in self._websockets:
                # websocket
                websocket = self._websockets[stream_id]
                websocket.http_event_received(event)

            elif event.push_id in self.pushes:
                # push
                self.pushes[event.push_id].append(event)

        elif isinstance(event, PushPromiseReceived):
            self.pushes[event.push_id] = deque()
            self.pushes[event.push_id].append(event)

    def quic_event_received(self, event: QuicEvent) -> None:
        #  pass event to the HTTP layer
        if self._http is not None:
            for http_event in self._http.handle_event(event):
                self.http_event_received(http_event)

    def cleanup_stream(self, stream_id: int) -> None:
        self._request_events.pop(stream_id, None)
        self._request_waiter.pop(stream_id, None)


