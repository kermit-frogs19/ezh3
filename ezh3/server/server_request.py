from typing import Optional, Dict
import json as json_lib


class ServerRequest:
    def __init__(
        self,
        method: str = None,
        body: bytes = b"",
        path: str = None,
        raw_headers: list = None,
        content_type: str = None,
        headers: dict = None

    ) -> None:
        self.body = body
        self.headers = headers
        self.method = method
        self.path = path
        self.raw_headers = raw_headers
        self.content_type = content_type

        self._process_headers()

    def json(self) -> dict:
        return json_lib.loads(str(self.body.decode()))

    @property
    def text(self):
        return str(self.body.decode())

    def _process_headers(self) -> None:
        if self.raw_headers:
            headers = {str(k.decode()).replace(":", ""): v.decode() for k, v in self.raw_headers}

            if "path" in headers and not self.path:
                self.path = headers["path"]
            if "method" in headers and not self.method:
                self.method = headers["method"]
            if "content-type" in headers and not self.content_type:
                self.content_type = headers["content-type"]

        else:
            headers = {}

        self.headers = headers

    def _process_body(self) -> None:
        pass



