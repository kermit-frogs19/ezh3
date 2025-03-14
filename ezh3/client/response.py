from dataclasses import dataclass, field


@dataclass
class Response:
    code: int = None
    data: dict = None
    headers: dict = None
    method: str = None
    url: str = None