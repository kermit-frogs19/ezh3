

class HTTPError(Exception):
    pass


class HTTPRequestError(HTTPError):
    pass


class HTTPTimeoutError(HTTPError):
    pass


class HTTPStatusError(HTTPError):
    def __init__(self, status_code: int, message: str = "", response=None):
        self.status_code = status_code
        self.message = message
        self.response = response
        super().__init__()




