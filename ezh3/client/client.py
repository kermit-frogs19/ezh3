import argparse
import asyncio
import logging
import os
import pickle
import ssl
import time
import socket
from collections import deque
from typing import BinaryIO, Callable, Deque, Dict, List, Optional, Union, cast
from urllib.parse import urlparse
from pathlib import Path

import aioquic
from aioquic.quic.connection import QuicConnection, QuicTokenHandler
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.connection import END_STATES
from aioquic.h3.connection import H3_ALPN, ErrorCode, H3Connection
from aioquic.h3.events import (
    DataReceived,
    H3Event,
    HeadersReceived,
    PushPromiseReceived,
)
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent
from aioquic.tls import SessionTicket


from ezh3.client.websocket import WebSocket
from ezh3.client.url import URL
from ezh3.client.request import Request
from ezh3.client.response import Response
from ezh3.client.connection import Connection


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.DEBUG,
)


logger = logging.getLogger("client")


USER_AGENT = "aioquic/" + aioquic.__version__
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 29024


class Client:
    def __init__(
            self,
            base_url: str = "",
            headers: dict = None,
            use_tls: bool = False
    ):
        self.base_url = base_url
        self.headers = headers or {}
        self.use_tls = use_tls
        self.connections: list[Connection] = []

        self._is_running: bool = True
        self._session_tickets = []

    async def __aenter__(self):
        self._is_running = True
        if self.base_url:
            url = URL(self.base_url)
            await self._connect(url.port, url.host)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @property
    def is_running(self):
        return self._is_running

    async def close(self):
        for connection in self.connections:
            await connection.aclose()
            self.connections.remove(connection)
        self._is_running = False

    async def get(self, url: str, headers: dict = None) -> Response:
        """
        Perform a GET request.
        """
        return await self._request(
            Request(method="GET", url=URL(url), headers=headers)
        )

    async def post(self, url: str, data: bytes, headers: dict = None) -> Response:
        """
        Perform a POST request.
        """
        return await self._request(
            Request(method="POST", url=URL(url), content=data, headers=headers)
        )

    async def patch(self, url: str, data: bytes, headers: dict = None) -> Response:
        """
        Perform a POST request.
        """
        return await self._request(
            Request(method="PATCH", url=URL(url), content=data, headers=headers)
        )

    async def put(self, url: str, data: bytes, headers: dict = None) -> Response:
        """
        Perform a POST request.
        """
        return await self._request(
            Request(method="PUT", url=URL(url), content=data, headers=headers)
        )

    async def delete(self, url: str, headers: dict = None) -> Response:
        """
        Perform a POST request.
        """
        return await self._request(
            Request(method="DELETE", url=URL(url), headers=headers)
        )

    async def request(self, method: str, url: str, data: bytes, headers: dict = None) -> Response:
        """
        Perform a POST request.
        """
        return await self._request(
            Request(method=method, url=URL(url), content=data, headers=headers)
        )

    async def websocket(self, url: str, subprotocols: Optional[List[str]] = None) -> WebSocket:
        """
        Open a WebSocket.
        """
        request = Request(method="CONNECT", url=URL(url))

        connection = await self._connect(request.url.port, request.url.host)
        stream_id = connection._quic.get_next_available_stream_id()
        websocket = WebSocket(http=connection._http, stream_id=stream_id, transmit=connection.transmit)
        connection._websockets[stream_id] = websocket

        headers = [
            (b":method", b"CONNECT"),
            (b":scheme", b"https"),
            (b":authority", request.url.authority.encode()),
            (b":path", request.url.full_path.encode()),
            (b":protocol", b"websocket"),
            (b"user-agent", USER_AGENT.encode()),
            (b"sec-websocket-version", b"13"),
        ]
        if subprotocols:
            headers.append(
                (b"sec-websocket-protocol", ", ".join(subprotocols).encode())
            )
        connection._http.send_headers(stream_id=stream_id, headers=headers)
        connection.transmit()

        return websocket

    async def _connect(
            self, port: int,
            host: str,
            wait_connected: bool = True,
            local_port: int = 0
    ) -> Connection:
        protocol = next((con for con in self.connections if con.port == port and con.host == host), None)
        if protocol:
            if not protocol.is_running:
                self.connections.remove(protocol)
            else:
                return protocol

        # prepare configuration
        config = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536)
        config.server_name = host

        if not self.use_tls:
            config.verify_mode = ssl.CERT_NONE

        loop = asyncio.get_event_loop()
        local_host = "::"

        # lookup remote address
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        addr = infos[0][4]
        if len(addr) == 2:
            addr = ("::ffff:" + addr[0], addr[1], 0, 0)

        connection_ = QuicConnection(configuration=config, session_ticket_handler=self.save_session_ticket)

        # explicitly enable IPv4/IPv6 dual stack
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        completed = False
        try:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind((local_host, local_port, 0, 0))
            completed = True
        finally:
            if not completed:
                sock.close()
        # connect
        transport, protocol = await loop.create_datagram_endpoint(lambda: Connection(connection_), sock=sock)
        protocol = cast(Connection, protocol)

        try:
            protocol.connect(addr, transmit=wait_connected)
            if wait_connected:
                await protocol.wait_connected()
            protocol.transport = transport
            protocol.port = port
            protocol.host = host
            self.connections.append(protocol)
            return protocol
        except:
            protocol.close()
            await protocol.wait_closed()
            transport.close()

    async def _request(self, request: Request) -> Response:
        connection = await self._connect(request.url.port, request.url.host)
        self._is_running = True

        stream_id = connection._quic.get_next_available_stream_id()
        connection._http.send_headers(
            stream_id=stream_id,
            headers=[
                        (b":method", request.method.encode()),
                        (b":scheme", request.url.scheme.encode()),
                        (b":authority", request.url.authority.encode()),
                        (b":path", request.url.full_path.encode()),
                        (b"user-agent", USER_AGENT.encode()),
                    ]
                    + [(k.encode(), v.encode()) for (k, v) in request.headers.items()],
            end_stream=not request.content,
        )
        if request.content:
            connection._http.send_data(stream_id=stream_id, data=request.content, end_stream=True)

        waiter = connection._loop.create_future()
        connection._request_events[stream_id] = deque()
        connection._request_waiter[stream_id] = waiter
        connection.transmit()

        events = await asyncio.shield(waiter)

        response = Response(url=request.url.full_path, method=request.method)

        for event in events:
            if isinstance(event, DataReceived):
                response.data = event.data.decode()
            elif isinstance(event, HeadersReceived):
                response.headers = event.headers
                for header, value in event.headers:
                    if header == b":status":
                        response.code = int(value.decode())
        return response

    def save_session_ticket(self, ticket: SessionTicket) -> None:
        """
        Callback which is invoked by the TLS engine when a new session ticket
        is received.
        """
        self._session_tickets.append(ticket)
        print(f"New session ticket received: {ticket}")