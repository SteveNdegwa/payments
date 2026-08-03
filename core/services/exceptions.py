class InboundPaymentError(Exception):
    """Raised when an inbound payment operation violates its lifecycle rules."""


class OutboundTransferError(Exception):
    """Raised when an outbound transfer operation cannot be performed."""
