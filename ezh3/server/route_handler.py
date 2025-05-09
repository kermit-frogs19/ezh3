from dataclasses import dataclass, field
from typing import Callable
from inspect import signature as signature
from types import MappingProxyType


@dataclass
class RouteHandler:
    method: str = field(default="")
    function: Callable = field(default=lambda: None)
    parameters: MappingProxyType = field(default=None)

    def __post_init__(self):
        self.parameters = signature(self.function).parameters