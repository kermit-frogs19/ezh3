from dataclasses import dataclass, field
from typing import Callable
from inspect import signature as signature
from types import MappingProxyType


@dataclass
class RequestHandler:
    method: str = field(default="")
    function: Callable = field(default=lambda: None)
    params: list = field(default_factory=list)
    parameters: MappingProxyType = field(default=None)

    def __post_init__(self):
        self.parameters = signature(self.function).parameters