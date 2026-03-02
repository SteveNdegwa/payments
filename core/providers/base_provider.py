import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ProviderResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PENDING = "PENDING"
    REQUIRES_ACTION = "REQUIRES_ACTION"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ProviderResult:
    status: ProviderResultStatus
    provider_transaction_id: str = ""
    provider_reference: str = ""
    raw_response: dict = field(default_factory=dict)
    failure_code: str = ""
    failure_reason: str = ""
    # For REQUIRES_ACTION flows (3DS redirect, STK push, etc.)
    next_action: Optional[dict] = None
    # Amount actually processed (for partial captures, etc.)
    amount_processed: Optional[Decimal] = None


class BaseProvider:
    def __init__(self, credentials: dict, config: dict | None = None):
        self.credentials = credentials
        self.config = config or {}


    # Core payment flows
    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        """Single-step charge (mobile money, wallets, etc.)."""
        raise NotImplementedError

    def authorize(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        """Auth-only (cards with authorize-capture flow)."""
        raise NotImplementedError

    def capture(self, *, provider_transaction_id: str, amount: Decimal, payload: dict) -> ProviderResult:
        """Capture a previously authorised amount."""
        raise NotImplementedError

    def void(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        """Void / reverse an authorisation before capture."""
        raise NotImplementedError

    def refund(self, *, provider_transaction_id: str, amount: Decimal, payload: dict) -> ProviderResult:
        """Full or partial refund on a captured transaction."""
        raise NotImplementedError


    # Reconciliation
    def query_status(self, *, provider_transaction_id: str) -> ProviderResult:
        """Poll provider for the current status of a transaction."""
        raise NotImplementedError


    # Webhook / callback verification
    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        """Verify an inbound provider callback is authentic."""
        raise NotImplementedError

    def parse_callback(self, *, payload: dict) -> ProviderResult:
        """Translate provider callback payload into a ProviderResult."""
        raise NotImplementedError
