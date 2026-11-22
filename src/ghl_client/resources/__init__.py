"""ghl-client resources."""

from .contacts import ContactsResource, IdempotencyLedger
from .custom_fields import CustomFieldsResource
from .opportunities import OpportunitiesResource

__all__ = [
    "ContactsResource",
    "CustomFieldsResource",
    "IdempotencyLedger",
    "OpportunitiesResource",
]
