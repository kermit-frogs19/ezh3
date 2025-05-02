from typing import Literal
import aioquic


class _DEFAULT_TIMEOUT(int):
    pass


DEFAULT_TIMEOUT = _DEFAULT_TIMEOUT(5)

ALLOWED_METHODS: list[str] = ["GET", "POST", "PATCH", "PUT", "DELETE"]

AllowedMethods = Literal["GET", "POST", "PATCH", "PUT", "DELETE"]

USER_AGENT = "aioquic/" + aioquic.__version__