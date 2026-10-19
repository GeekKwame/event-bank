import logging
import uuid
from collections.abc import Sequence

from apps.common.phone import mask_phone
from apps.notifications.providers.base import DeliveryOutcome, NotificationProvider, SendResult

logger = logging.getLogger(__name__)


class ConsoleSMSProvider(NotificationProvider):
    """Development provider: logs messages instead of sending them."""

    name = "console"
    channel = "sms"

    def send(self, recipients: Sequence[str], message: str) -> SendResult:
        message_id = f"console-{uuid.uuid4()}"
        logger.info(
            "SMS %s to %s: %s",
            message_id,
            ", ".join(mask_phone(recipient) for recipient in recipients),
            message,
        )
        return SendResult(
            provider=self.name,
            outcome=DeliveryOutcome.ACCEPTED,
            provider_message_id=message_id,
            accepted_recipients=tuple(recipients),
        )
