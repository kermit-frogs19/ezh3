import argparse
import asyncio
import logging
import os
import pickle
import json as json_lib
import ssl
import time
import socket
from collections import deque
from typing import BinaryIO, Callable, Deque, Dict, List, Optional, Union, cast, Literal
from urllib.parse import urlparse
from pathlib import Path
import certifi

import aioquic
from aioquic.quic.connection import QuicConnection, QuicTokenHandler
from aioquic.asyncio.client import connect
from aioquic.asyncio.protocol import QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent
from aioquic.tls import SessionTicket
from aioquic.quic.connection import END_STATES
from aioquic.h3.connection import H3_ALPN, ErrorCode, H3Connection
from aioquic.h3.events import DataReceived, H3Event, HeadersReceived, PushPromiseReceived

from ezh3.client.client_websocket import ClientWebSocket
from ezh3.client.url import URL
from ezh3.client.client_request import ClientRequest
from ezh3.client.client_response import ClientResponse
from ezh3.client.client_connection import ClientConnection
from ezh3.client.exceptions import *
from ezh3.common.config import _DEFAULT_TIMEOUT, DEFAULT_TIMEOUT, USER_AGENT


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.DEBUG,
)


logger = logging.getLogger("client")


class Client:
    def __init__(
            self,
            base_url: str = "",
            headers: dict = None,
            use_tls: bool = True,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ):
        self.raw_base_url = base_url
        self.base_url = URL(base_url)
        self.headers = headers or {}
        self.use_tls = use_tls
        self.timeout = timeout

        self.connections: set[ClientConnection] = set()
        self._is_running: bool = True

    async def __aenter__(self):
        self._is_running = True
        if self.raw_base_url:
            await self.connect(self.base_url.port, self.base_url.host)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @property
    def is_running(self):
        return self._is_running

    async def close(self, error_code=None):
        await asyncio.gather(*(conn.aclose(error_code) for conn in self.connections))  # ✅ Close all in parallel
        self.connections.difference_update(self.connections)  # ✅ Remove all at once
        self._is_running = False

    async def get(
            self,
            url: str,
            data: bytes = None,
            headers: dict = None,
            json: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT,
    ) -> ClientResponse:
        """
        Perform a GET request.
        """
        return await self._request(ClientRequest(
            method="GET",
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def post(
            self,
            url: str,
            data: bytes = None,
            headers: dict = None,
            json: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ) -> ClientResponse:
        """
        Perform a POST request.
        """

        return await self._request(ClientRequest(
            method="POST",
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def patch(
            self,
            url: str,
            data: bytes,
            headers: dict = None,
            json: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT

    ) -> ClientResponse:
        """
        Perform a PATCH request.
        """

        return await self._request(ClientRequest(
            method="PATCH",
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def put(
            self,
            url: str,
            data: bytes,
            headers: dict = None,
            json: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ) -> ClientResponse:
        """
        Perform a PUT request.
        """
        return await self._request(ClientRequest(
            method="PUT",
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def delete(
            self,
            url: str,
            headers: dict = None,
            data: bytes = None,
            json: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ) -> ClientResponse:
        """
        Perform a DELETE request.
        """
        return await self._request(ClientRequest(
            method="DELETE",
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def request(
            self,
            method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"],
            url: str,
            data: bytes = None,
            json: dict = None,
            headers: dict = None,
            timeout: int | float | None = DEFAULT_TIMEOUT
    ) -> ClientResponse:
        """
        Perform a request.
        """
        return await self._request(ClientRequest(
            method=method,
            url=url,
            content=data,
            json=json,
            headers=headers,
            timeout=timeout
        ))

    async def websocket(self, url: str, subprotocols: Optional[List[str]] = None) -> ClientWebSocket:
        """
        Open a WebSocket.
        """
        request = ClientRequest(method="CONNECT", url=url)

        connection = await self.connect(request.url.port, request.url.host)
        stream_id = connection._quic.get_next_available_stream_id()
        websocket = ClientWebSocket(http=connection._http, stream_id=stream_id, transmit=connection.transmit)
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

    async def connect(
            self, port: int,
            host: str,
            wait_connected: bool = True,
            local_port: int = 0
    ) -> ClientConnection:
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
        else:
            config.verify_mode = ssl.CERT_REQUIRED
            config.load_verify_locations(cafile=certifi.where())

        loop = asyncio.get_event_loop()
        local_host = "::"

        # lookup remote address
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        addr = infos[0][4]
        if len(addr) == 2:
            addr = ("::ffff:" + addr[0], addr[1], 0, 0)

        connection_ = QuicConnection(configuration=config, session_ticket_handler=self._save_session_ticket)

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
        transport, protocol = await loop.create_datagram_endpoint(lambda: ClientConnection(connection_), sock=sock)
        protocol = cast(ClientConnection, protocol)

        try:
            protocol.connect(addr, transmit=wait_connected)
            if wait_connected:
                await protocol.wait_connected()
            protocol.transport = transport
            protocol.port = port
            protocol.host = host
            self.connections.add(protocol)
            return protocol
        except:
            protocol.close()
            await protocol.wait_closed()
            transport.close()

    async def _request(self, request: ClientRequest) -> ClientResponse:
        # Resolve the conflict between class base URL and request URL
        request.url = self.base_url.resolve(request.url)

        connection = await self.connect(request.url.port, request.url.host)
        self._is_running = True

        timeout = request.timeout if not isinstance(request.timeout, _DEFAULT_TIMEOUT) else self.timeout

        stream_id = connection._quic.get_next_available_stream_id()
        connection._http.send_headers(stream_id=stream_id, headers=request.render_headers(), end_stream=request.is_empty)

        if not request.is_empty:
            connection._http.send_data(stream_id=stream_id, data=request.body, end_stream=True)

        waiter = connection._loop.create_future()
        connection._request_events[stream_id] = deque()
        connection._request_waiter[stream_id] = waiter
        connection.transmit()
        try:
            events = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError:
            raise HTTPTimeoutError(f"Request timed out after {timeout} seconds")

        return self._process_response_events(request=request, events=events)

    def _process_response_events(self, request: ClientRequest, events: list[H3Event]) -> ClientResponse:
        raw_headers = None
        body = bytearray()

        for event in events:
            if isinstance(event, HeadersReceived):
                raw_headers = event.headers
            elif isinstance(event, DataReceived):
                body.extend(event.data)

        return ClientResponse(raw_headers=raw_headers, request=request, body=bytes(body))

    def _save_session_ticket(self, ticket: SessionTicket) -> None:
        """
        Callback which is invoked by the TLS engine when a new session ticket
        is received.
        """
        print(f"New session ticket received: {ticket}")


async def main1():
    url = "https://vadim-seliukov-quic-server.com"
    data = {"voice": "Hellooo"}
    client = Client(url, use_tls=True)
    response = None
    try:
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)
        response = await client.post("/echo", json=data, timeout=None)





        response.raise_for_status()
    except HTTPTimeoutError as e:
        print(e)
    except HTTPStatusError as e:
        print(e)

    if response is None:
        return
    print(response)
    data = response.json()
    await client.close()
    print("success")


if __name__ == "__main__":
    asyncio.run(main1())


# async def perform_http_request(
#     client: Connection,
#     url: str,
#     data: Optional[str],
#     include: bool,
# ) -> None:
#     # perform request
#     if data is not None:
#         data_bytes = data.encode()
#         response = await client.post(
#             url,
#             data=data_bytes,
#             headers={
#                 "content-length": str(len(data_bytes)),
#                 "content-type": "application/x-www-form-urlencoded",
#             },
#         )
#         method = "POST"
#     else:
#         response = await client.get(url)
#         method = "GET"
#
#     print(response)
#
#
#     # # print speed
#     # octets = 0
#     # for http_event in http_events:
#     #     if isinstance(http_event, DataReceived):
#     #         octets += len(http_event.data)
#     # logger.info(
#     #     "Response received for %s %s : %d bytes in %.1f s (%.3f Mbps)"
#     #     % (method, urlparse(url).path, octets, elapsed, octets * 8 / elapsed / 1000000)
#     # )
#     # # output response
#     # write_response(http_events=http_events, include=include)
#
#
# def process_http_pushes(
#     client: Connection,
#     include: bool,
# ) -> None:
#     for _, http_events in client.pushes.items():
#         method = ""
#         octets = 0
#         path = ""
#         for http_event in http_events:
#             if isinstance(http_event, DataReceived):
#                 octets += len(http_event.data)
#             elif isinstance(http_event, PushPromiseReceived):
#                 for header, value in http_event.headers:
#                     if header == b":method":
#                         method = value.decode()
#                     elif header == b":path":
#                         path = value.decode()
#         logger.info("Push received for %s %s : %s bytes", method, path, octets)
#
#         # output response
#         write_response(http_events=http_events, include=include)
#
#
# def write_response(
#     http_events: Deque[H3Event], include: bool
# ) -> None:
#     for http_event in http_events:
#         if isinstance(http_event, HeadersReceived) and include:
#             headers = b""
#             for k, v in http_event.headers:
#                 headers += k + b": " + v + b"\r\n"
#             if headers:
#                 print("write_response", headers + b"\r\n")
#                 # output_file.write(headers + b"\r\n")
#         elif isinstance(http_event, DataReceived):
#             print(f"write_response: {http_event.data}")
#             # output_file.write(http_event.data)
#
#
# def save_session_ticket(ticket: SessionTicket) -> None:
#     """
#     Callback which is invoked by the TLS engine when a new session ticket
#     is received.
#     """
#     print(f"New session ticket received: {ticket}")
#
#
# async def main(
#     urls: List[str],
#     data: Optional[str],
#     include: bool,
#     local_port: int,
#     zero_rtt: bool,
# ) -> None:
#     # parse URL
#     parsed = urlparse(urls[0])
#     assert parsed.scheme in (
#         "https",
#         "wss",
#     ), "Only https:// or wss:// URLs are supported."
#     host = parsed.hostname
#     if parsed.port is not None:
#         port = parsed.port
#     else:
#         port = 443
#
#     # check validity of 2nd urls and later.
#     for i in range(1, len(urls)):
#         _p = urlparse(urls[i])
#
#         # fill in if empty
#         _scheme = _p.scheme or parsed.scheme
#         _host = _p.hostname or host
#         _port = _p.port or port
#
#         assert _scheme == parsed.scheme, "URL scheme doesn't match"
#         assert _host == host, "URL hostname doesn't match"
#         assert _port == port, "URL port doesn't match"
#
#         # reconstruct url with new hostname and port
#         _p = _p._replace(scheme=_scheme)
#         _p = _p._replace(netloc="{}:{}".format(_host, _port))
#         _p = urlparse(_p.geturl())
#         urls[i] = _p.geturl()
#
#     # prepare configuration
#     configur = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN, max_datagram_frame_size=65536)
#     configur.verify_mode = ssl.CERT_NONE
#
#     async with connect(SERVER_HOST, SERVER_PORT, configuration=configur, create_protocol=Connection, session_ticket_handler=save_session_ticket) as client:
#         client = cast(Connection, client)
#
#         if parsed.scheme == "wss":
#             ws = await client.websocket(urls[0], subprotocols=["chat", "superchat"])
#
#             # send some messages and receive reply
#             for i in range(2):
#                 message = "Hello {}, WebSocket!".format(i)
#                 print("> " + message)
#                 await ws.send(message)
#
#                 message = await ws.recv()
#                 print("< " + message)
#
#             await ws.close()
#         else:
#             # perform request
#             coros = [
#                 perform_http_request(
#                     client=client,
#                     url=url,
#                     data=data,
#                     include=include,
#                 )
#                 for url in urls
#             ]
#             await asyncio.gather(*coros)
#
#             # process http pushes
#             process_http_pushes(client=client, include=include)
#         client.close(error_code=ErrorCode.H3_NO_ERROR)
#
#
#
#
#
#
#     # asyncio.run(
#     #     main(
#     #         urls=["https://127.0.0.1:29024"],
#     #         data=None,
#     #         include=True,
#     #         local_port=0,
#     #         zero_rtt=False,
#     #     )
#     # )
#

