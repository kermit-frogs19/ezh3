from dataclasses import dataclass, field
from typing import Any
import json as json_lib


@dataclass
class Response:
    status_code: int = field(default=200)
    content: Any = field(default=None)
    headers: dict = field(default_factory=dict)
    url: str = field(default="")
    content_type: str = field(default="application/octet-stream")
    charset: str = field(default="utf-8")
    _content_length: int = field(default=0)
    _header_encoding: str = field(default="latin-1")

    @property
    def content_length(self) -> int:
        if self._content_length:
            return self._content_length

        length = 0
        if self.content is not None and self._content_length == 0:
            length = len(self.render_body())
        return length

    def render_headers(self) -> list:
        raw_headers = [
            (b":status", str(self.status_code).encode(self._header_encoding)),
            (b"content-type", self.content_type.encode(self._header_encoding)),
            (b"content-length", str(self.content_length).lower().encode(self._header_encoding))
        ]

        for header, value in self.headers.items():
            if header.lower() == "content-type":
                if value.lower() != self.content_type and self.content_type != "application/octet-stream":
                    raise ValueError(f"Unsupported request content type for class {self.__class__.__name__}")
            if header.lower() == "content-length":
                continue

            raw_headers.append((header.lower().encode(self._header_encoding), value.lower().encode(self._header_encoding)))

        return raw_headers

    def render_body(self) -> bytes:
        if isinstance(self.content, bytes):
            body_bytes = self.content
        elif isinstance(self.content, str):
            body_bytes = self.content.encode(self.charset)
        elif isinstance(self.content, (dict, list)):
            body_bytes = json_lib.dumps(self.content).encode(self.charset)
        else:
            body_bytes = b""
        return body_bytes


@dataclass
class JSONResponse(Response):
    content: dict = field(default_factory=dict)
    content_type: str = field(default="application/json", init=False)


@dataclass
class TextResponse(Response):
    content: str = field(default="")
    content_type: str = field(default="text/plain", init=False)


@dataclass
class HTMLResponse(Response):
    content: str = field(default="")
    content_type: str = field(default="text/html", init=False)




