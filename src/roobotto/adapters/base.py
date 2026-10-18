"""The output adapter interface.

An adapter turns bytes into a send on one port and reports what it knows about the result. It
makes no decisions: the output supervisor decides what reaches which adapter.
"""

from typing import Literal, Protocol

AdapterName = Literal["mock", "midi"]
SendResult = Literal["attempted", "confirmed", "failed", "unknown"]


class OutputAdapter(Protocol):
    @property
    def name(self) -> AdapterName: ...

    def send(self, port: str, data: bytes, label: str) -> SendResult: ...
