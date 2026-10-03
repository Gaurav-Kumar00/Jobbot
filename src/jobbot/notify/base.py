"""Channel-agnostic notification types. New channels implement `Notifier`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    url: str


@dataclass(frozen=True)
class OutgoingMessage:
    text: str  # Telegram-flavoured HTML
    buttons: tuple[Button, ...] = field(default_factory=tuple)


class Notifier(Protocol):
    async def send(self, message: OutgoingMessage) -> str:
        """Deliver the message and return the channel's message id."""
        ...
