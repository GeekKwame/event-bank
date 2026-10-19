"""Channel-agnostic provider contract. Provider-specific code lives in sibling modules."""

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar


class DeliveryOutcome(enum.StrEnum):
    ACCEPTED = "accepted"
    """The provider accepted the message for delivery."""

    REJECTED = "rejected"
    """The provider refused the message; retrying the same request will not help."""

    RETRYABLE = "retryable"
    """The request definitely did not reach the provider, or was refused before processing."""

    UNKNOWN = "unknown"
    """The provider may or may not have sent the message (e.g. a timeout after sending).
    Never retry automatically: that risks a duplicate message."""

    @property
    def may_retry_automatically(self) -> bool:
        return self is DeliveryOutcome.RETRYABLE


@dataclass(frozen=True)
class SendResult:
    provider: str
    outcome: DeliveryOutcome
    provider_message_id: str | None = None
    accepted_recipients: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)


class ProviderConfigurationError(Exception):
    pass


class SendingDisabledError(ProviderConfigurationError):
    pass


class NotificationProvider(ABC):
    name: ClassVar[str]
    channel: ClassVar[str]

    @abstractmethod
    def send(self, recipients: Sequence[str], message: str) -> SendResult: ...
