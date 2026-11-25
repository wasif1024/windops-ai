"""How an email leaves, behind one small interface, so a provider can be added without changing
anything else.

`record` is the only sender for now (ADR 0037): it renders the email and keeps it as `recorded`,
sending nothing, as no email provider is configured. Phase 15's Azure deployment can add Azure
Communication Services behind the same interface. The message holds no address: a sender looks
the recipient's up when it sends, so this service never keeps one (FR-NOT-002).
"""

from dataclasses import dataclass
from typing import Protocol

from windops_notification.store import RECORDED


@dataclass(frozen=True)
class Email:
    tenant_id: str
    user_id: str
    subject: str
    text: str


class Sender(Protocol):
    name: str

    async def send(self, email: Email) -> str:
        """The delivery's status once sent. Raises SendError when it could not be."""


class SendError(Exception):
    """Not sent this time. It is tried again later, a few times."""


class Recording:
    """Renders and keeps each email, sending nothing: there is no email provider here."""

    name = "record"

    def __init__(self) -> None:
        self.kept: list[Email] = []

    async def send(self, email: Email) -> str:
        # Kept in memory only for tests; the rendered text is the notification's own.
        if len(self.kept) < 1000:
            self.kept.append(email)
        return RECORDED


def sender(setting: str) -> Sender:
    if setting in ("", "record"):
        return Recording()
    raise ValueError(f"WINDOPS_NOTIFICATION_EMAIL must be record, not {setting!r}")
