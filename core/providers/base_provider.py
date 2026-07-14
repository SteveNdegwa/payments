import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

logger = logging.getLogger(__name__)


class ProviderResultStatus(StrEnum):
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
    next_action: dict | None = None
    # Amount actually processed (for partial captures, etc.)
    amount_processed: Decimal | None = None


class BaseProvider(ABC):
    def __init__(self, credentials: dict, config: dict | None = None):
        self.credentials = credentials
        self.config = config or {}

    def _unsupported_operation(self, operation: str) -> ProviderResult:
        return ProviderResult(
            status=ProviderResultStatus.FAILED,
            failure_code="unsupported_operation",
            failure_reason=f"{self.__class__.__name__} does not support {operation}.",
        )

    # Core payment flows
    def charge(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        """Single-step charge (mobile money, wallets, etc.)."""
        return self._unsupported_operation("charge")

    def authorize(self, *, amount: Decimal, currency: str, payload: dict) -> ProviderResult:
        """Auth-only (cards with authorize-capture flow)."""
        return self._unsupported_operation("authorization")

    def capture(
        self, *, provider_transaction_id: str, amount: Decimal, payload: dict
    ) -> ProviderResult:
        """Capture a previously authorised amount."""
        return self._unsupported_operation("capture")

    def void(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        """Void / reverse an authorisation before capture."""
        return self._unsupported_operation("void")

    def refund(
        self, *, provider_transaction_id: str, amount: Decimal, payload: dict
    ) -> ProviderResult:
        """Full or partial refund on a captured transaction."""
        return self._unsupported_operation("refund")

    # Reconciliation
    def query_status(self, *, provider_transaction_id: str, payload: dict) -> ProviderResult:
        """Poll provider for the current status of a transaction."""
        return self._unsupported_operation("status query")

    # Webhook / callback verification
    @abstractmethod
    def verify_callback(self, *, headers: dict, payload: dict) -> bool:
        """Verify an inbound provider callback is authentic."""
        raise NotImplementedError

    @abstractmethod
    def parse_callback(self, *, payload: dict) -> ProviderResult:
        """Translate provider callback payload into a ProviderResult."""
        raise NotImplementedError
