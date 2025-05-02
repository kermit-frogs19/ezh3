from typing import Optional, Dict
from dataclasses import dataclass, field
from typing import Literal
import json as json_lib
import aioquic

from ezh3.client.url import URL
from ezh3.common.config import DEFAULT_TIMEOUT


@dataclass
class ClientRequest:
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE", "CONNECT"] = field(default="GET")
    url: str | URL = field(default=None)
    content: bytes = field(default=b"")
    json: dict = field(default=None)
    headers: dict = field(default=None)
    charset: str = field(default="utf-8")
    timeout: int | float = field(default=DEFAULT_TIMEOUT)

    body: bytes = field(default=b"", init=False)
    _user_agent: str = field(default="aioquic/" + aioquic.__version__, init=False)
    _content_length: int = field(default=0, init=False)
    _header_encoding: str = field(default="latin-1", init=False)

    def __post_init__(self) -> None:
        if self.content and self.json:
            raise ValueError("Cannot have both 'json' and 'content' parameters specified")

        self.json = {} if not self.json else self.json
        self.headers = {} if not self.headers else self.headers

        self.url = URL(self.url)
        self.body = self.render_body()

    @property
    def is_empty(self) -> bool:
        return not self.body

    def render_body(self) -> bytes:
        if self.content:
            body = self.content
        elif self.json:
            body = json_lib.dumps(self.json).encode(self.charset)
        else:
            body = b""

        return body

    def render_headers(self) -> list[tuple]:
        headers = [
            (b":method", self.method.encode(self._header_encoding)),
            (b":scheme", self.url.scheme.encode(self._header_encoding)),
            (b":authority", self.url.authority.encode(self._header_encoding)),
            (b":path", self.url.full_path.encode(self._header_encoding)),
            (b"user-agent", self._user_agent.encode(self._header_encoding)),
            (b"content-length", str(len(self.body)).encode(self._header_encoding)),
        ]

        content_type = next((value.lower() for header, value in self.headers.items() if header.lower() == "content-type"), None)
        if not content_type and self.json:
            content_type = "application/json"

        headers.append((b"content-type", content_type.encode(self._header_encoding)))
        headers.extend([(k.lower(self._header_encoding).encode(), v.encode()) for (k, v) in self.headers.items() if k.lower() not in {"content-type", "content-length"}])

        return headers


