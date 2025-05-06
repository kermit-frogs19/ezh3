import json as json_lib
from dataclasses import dataclass, field

from ezh3.client.client_request import ClientRequest
from ezh3.client.url import URL
from ezh3.client.exceptions import *


@dataclass
class ClientResponse:
    status_code: int = field(default=None)
    method: str = field(default="")
    headers: dict = field(default=None)
    url: URL = field(default=None)
    body: bytes = field(default=b"")
    path: str = field(default="")
    request: ClientRequest = field(default=None)
    raw_headers: list[tuple] = field(default=None)
    content_type: str = field(default=None)
    encoding: str = field(default="utf-8")
    reason: str = field(default=None)
    content_length: int = field(default=0, init=False)
    _header_encoding: str = field(default="latin-1", init=False)

    def __post_init__(self) -> None:
        self.headers = self._process_headers()
        if not self.url:
            self.url = self.request.url
        if not self.path:
            self.path = self.request.url.full_path
        if not self.method:
            self.method = self.request.method

        self.analyze_status_code()

    @property
    def text(self):
        return str(self.body.decode())

    @property
    def ok(self) -> bool:
        return 400 > self.status_code

    @property
    def is_bad_request(self) -> bool:
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        return 500 <= self.status_code < 600

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400

    def json(self) -> dict:
        return json_lib.loads(str(self.body.decode()).replace("'", '"'))

    def _process_headers(self) -> dict:
        if self.raw_headers:
            headers = {str(k.decode()).replace(":", ""): v.decode() for k, v in self.raw_headers}

            if "path" in headers and not self.path:
                self.path = headers["path"]
            if "method" in headers and not self.method:
                self.method = headers["method"]
            if "content-type" in headers and not self.content_type:
                self.content_type = headers["content-type"]
            if "content-length" in headers and not self.content_length:
                self.content_length = int(headers["content-length"])
            if "status" in headers:
                self.status_code = int(headers["status"])
            else:
                self.content_length = len(self.body)

        else:
            headers = {}

        return headers

    def raise_for_status(self):
        if self.status_code is None:
            raise HTTPRequestError("No status code received")

        if self.ok:
            return

        if self.is_server_error or self.is_bad_request:
            raise HTTPStatusError(
                status_code=self.status_code,
                message=f"HTTP Error {self.status_code} - {self.reason}",
                response=self
            )

    def analyze_status_code(self) -> None:
        if self.reason is not None:
            return

        self.reason = {
            200: "success",
            201: "created",
            204: "no content",
            301: "moved permanently",
            302: "found",
            307: "temporary redirect",
            308: "permanent redirect",
            400: "bad request",
            401: "unauthorized",
            403: "forbidden",
            404: "not found",
            405: "method not allowed",
            408: "request timeout",
            409: "conflict",
            422: "unprocessable content",
            429: "too many requests",
            500: "internal server error",
            501: "not implemented",
            502: "bad gateway",
            503: "service unavailable",
            505: "http version not supported",
        }.get(self.status_code)

        if self.reason is not None:
            return

        if 200 <= self.status_code < 300:
            self.reason = "success"
        elif 300 <= self.status_code < 400:
            self.reason = "redirect"
        elif 400 <= self.status_code < 500:
            self.reason = "bad request"
        elif 500 <= self.status_code < 600:
            self.reason = "server error"
        else:
            self.reason = "unknown"




