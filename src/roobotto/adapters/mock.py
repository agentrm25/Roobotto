"""The mock adapter: records what would have been sent and sends nothing (AGENTS.md rule 1)."""

from dataclasses import dataclass

from roobotto.adapters.base import AdapterName, SendResult


@dataclass(frozen=True)
class MockSend:
    port: str
    data: bytes
    label: str


class MockAdapter:
    def __init__(self) -> None:
        self.sent: list[MockSend] = []

    @property
    def name(self) -> AdapterName:
        return "mock"

    def send(self, port: str, data: bytes, label: str) -> SendResult:
        self.sent.append(MockSend(port, data, label))
        return "attempted"
