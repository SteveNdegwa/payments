import hashlib
import json
import re
import secrets

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.safestring import mark_safe

from base.models import BaseModel


def generate_api_key():
    return secrets.token_urlsafe(32)


class System(BaseModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    hashed_api_key = models.CharField(max_length=255, editable=False, blank=True)
    allowed_ips = models.JSONField(default=list, blank=True)
    webhook_url = models.URLField()
    webhook_secret = models.CharField(max_length=255, null=True, blank=True)
    max_transaction_amount = models.DecimalField(
        max_digits=20, decimal_places=6, null=True, blank=True
    )
    allowed_currencies = models.JSONField(default=list, blank=True)
    rate_limit_per_minute = models.IntegerField(default=60)
    daily_volume_limit = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.hashed_api_key and not re.search(r"^[a-f0-9]{64}$", self.hashed_api_key):
            self.hashed_api_key = hashlib.sha256(self.hashed_api_key.encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)

    def verify_api_key(self, raw_key: str) -> bool:
        if not self.hashed_api_key:
            return False
        return self.hashed_api_key == hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


class PaymentMethodType(BaseModel):
    class Code(models.TextChoices):
        CARD = "CARD", "Card"
        MOBILE_MONEY = "MOBILE_MONEY", "Mobile Money"
        BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
        WALLET = "WALLET", "Wallet"
        CRYPTO = "CRYPTO", "Crypto"

    code = models.CharField(max_length=50, unique=True, choices=Code.choices)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Provider(BaseModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    class_name = models.CharField(max_length=255, unique=True)
    payment_method_types = models.ManyToManyField(
        PaymentMethodType,
        related_name="supported_by_providers",
        blank=True,
    )
    is_async = models.BooleanField(
        default=False,
        help_text="Provider responds via callback, not synchronously.",
    )
    supports_refund = models.BooleanField(default=False)
    supports_partial_refund = models.BooleanField(default=False)
    supports_authorize_capture = models.BooleanField(default=False)
    supports_3ds = models.BooleanField(default=False)
    reconciliation_timeout_seconds = models.IntegerField(
        default=300,
        help_text="Seconds to wait before polling the provider for a final status.",
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class ProviderAccount(BaseModel):
    class Environment(models.TextChoices):
        PRODUCTION = "PRODUCTION", "Production"
        SANDBOX = "SANDBOX", "Sandbox"

    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="accounts")
    name = models.CharField(max_length=255)
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.SANDBOX,
    )
    balance_tracking_enabled = models.BooleanField(
        default=False,
        help_text="Track the provider wallet balance as an operational snapshot.",
    )
    provider_available_balance = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
        editable=False,
        help_text="Latest provider-reported wallet balance; not an accounting ledger balance.",
    )
    balance_currency = models.CharField(max_length=3, blank=True, editable=False)
    balance_checked_at = models.DateTimeField(null=True, blank=True, editable=False)
    outbound_transfer_debit_account = models.ForeignKey(
        "LedgerAccount",
        on_delete=models.PROTECT,
        related_name="provider_accounts_debited_for_transfers",
        null=True,
        blank=True,
        help_text="Ledger account debited when an outbound transfer succeeds.",
    )
    outbound_transfer_credit_account = models.ForeignKey(
        "LedgerAccount",
        on_delete=models.PROTECT,
        related_name="provider_accounts_credited_for_transfers",
        null=True,
        blank=True,
        help_text="Ledger account credited when an outbound transfer succeeds.",
    )
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider"],
                condition=models.Q(is_default=True),
                name="unique_default_provider_account",
            )
        ]

    def __str__(self):
        return f"{self.provider.name} — {self.name}"

    def get_config(self) -> dict:
        return {entry.key: entry.typed_value for entry in self.config_entries.all()}


class ProviderAccountConfig(BaseModel):
    class ValueType(models.TextChoices):
        TEXT = "TEXT", "Text"
        BOOLEAN = "BOOLEAN", "Yes / No"
        INTEGER = "INTEGER", "Whole number"
        JSON = "JSON", "JSON object or list"

    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.CASCADE,
        related_name="config_entries",
    )
    key = models.CharField(max_length=100)
    value = models.TextField(
        help_text=(
            "Enter the value directly. For JSON, enter a valid object or list. "
            "For Yes / No, use true or false."
        )
    )
    value_type = models.CharField(
        max_length=20,
        choices=ValueType.choices,
        default=ValueType.TEXT,
    )

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider_account", "key"],
                name="unique_provider_account_config_key",
            )
        ]

    def __str__(self):
        return self.key

    def clean(self):
        super().clean()
        try:
            self.typed_value
        except (TypeError, ValueError) as exc:
            raise ValidationError(
                {"value": f"Value does not match {self.get_value_type_display()}."}
            ) from exc

    @property
    def typed_value(self):
        if self.value_type == self.ValueType.BOOLEAN:
            normalized = self.value.strip().lower()
            if normalized not in {"0", "1", "false", "no", "off", "on", "true", "yes"}:
                raise ValueError("Invalid boolean configuration value")
            return normalized in {"1", "true", "yes", "on"}
        if self.value_type == self.ValueType.INTEGER:
            return int(self.value)
        if self.value_type == self.ValueType.JSON:
            return json.loads(self.value)
        return self.value


class InboundPaymentEvent(BaseModel):
    class Flow(models.TextChoices):
        COLLECTION = "COLLECTION", "Single-step Collection"
        AUTHORIZE_CAPTURE = "AUTHORIZE_CAPTURE", "Authorize & Capture"

    class CallbackDestination(models.TextChoices):
        SYSTEM = "SYSTEM", "To owning System"
        SOURCE_SYSTEM = (
            "SOURCE_SYSTEM",
            "To the Source System that initiated the payment",
        )

    system = models.ForeignKey(
        System, on_delete=models.PROTECT, related_name="inbound_payment_events"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    provider = models.ForeignKey(
        Provider, on_delete=models.PROTECT, related_name="inbound_payment_events"
    )
    provider_account = models.ForeignKey(
        ProviderAccount, on_delete=models.PROTECT, related_name="inbound_payment_events"
    )
    flow = models.CharField(
        max_length=20,
        choices=Flow.choices,
        default=Flow.COLLECTION,
        help_text="Inbound payment flow used for this event.",
    )
    auto_capture = models.BooleanField(
        default=False,
        help_text=(
            "AUTHORIZE_CAPTURE flow only. Automatically capture after a successful authorization."
        ),
    )
    capture_delay_hours = models.IntegerField(
        default=0,
        help_text=(
            "Hours to wait before auto-capturing after authorization. "
            "0 means capture immediately. Only used when auto_capture=True."
        ),
    )
    fixed_amount = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    callback_destination = models.CharField(
        max_length=20,
        choices=CallbackDestination.choices,
        default=CallbackDestination.SYSTEM,
        help_text=mark_safe(
            "<b>Determines which system's webhook URL receives payment webhooks.</b><br><br>"
            "• <b>SYSTEM</b>: Always send to this InboundPaymentEvent.system.webhook_url.<br>"
            "• <b>SOURCE_SYSTEM</b>: Send to inbound_payment.source_system.webhook_url "
            "if it exists. If no source system is set, the webhook will fall back "
            "to the owning InboundPaymentEvent.system.webhook_url."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("system", "slug")

    def __str__(self):
        return f"{self.system.slug}/{self.slug}"


class InboundPaymentMethodToken(BaseModel):
    class TokenType(models.TextChoices):
        CARD = "CARD", "Card"
        MOBILE = "MOBILE", "Mobile Number"
        BANK_ACCOUNT = "BANK_ACCOUNT", "Bank Account"

    system = models.ForeignKey(
        System, on_delete=models.PROTECT, related_name="inbound_payment_method_tokens"
    )
    provider = models.ForeignKey(
        Provider, on_delete=models.PROTECT, related_name="inbound_payment_method_tokens"
    )
    token_type = models.CharField(max_length=20, choices=TokenType.choices)
    provider_token = models.CharField(max_length=500)
    masked_identifier = models.CharField(max_length=100, blank=True)
    card_brand = models.CharField(max_length=50, blank=True)
    expiry_month = models.CharField(max_length=2, blank=True)
    expiry_year = models.CharField(max_length=4, blank=True)
    customer_ref = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.token_type} {self.masked_identifier}"


class InboundPayment(BaseModel):
    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        REQUIRES_ACTION = "REQUIRES_ACTION", "Requires Customer Action"
        PROCESSING = "PROCESSING", "Processing"
        AUTHORIZED = "AUTHORIZED", "Authorized"
        CAPTURED = "CAPTURED", "Captured"
        PARTIALLY_CAPTURED = "PARTIALLY_CAPTURED", "Partially Captured"
        SETTLED = "SETTLED", "Settled"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        DISPUTED = "DISPUTED", "Disputed"
        REFUNDED = "REFUNDED", "Refunded"
        PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially Refunded"
        EXPIRED = "EXPIRED", "Expired"

    TERMINAL_STATUSES = frozenset(
        {
            Status.SETTLED,
            Status.FAILED,
            Status.CANCELLED,
            Status.REFUNDED,
            Status.EXPIRED,
        }
    )

    # The system that owns this inbound payment.
    system = models.ForeignKey(System, on_delete=models.PROTECT, related_name="inbound_payments")
    # The system that initiated this inbound payment.
    source_system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="initiated_inbound_payments",
        null=True,
        blank=True,
    )
    inbound_payment_event = models.ForeignKey(
        InboundPaymentEvent,
        on_delete=models.PROTECT,
        related_name="inbound_payments",
        null=True,
        blank=True,
    )
    payment_method_token = models.ForeignKey(
        InboundPaymentMethodToken,
        on_delete=models.PROTECT,
        related_name="inbound_payments",
        null=True,
        blank=True,
    )
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    amount_authorized = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    amount_captured = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    amount_refunded = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    amount_settled = models.DecimalField(max_digits=20, decimal_places=6, default=0)
    idempotency_key = models.CharField(max_length=255)
    provider_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider payload supplied at initialization.",
    )
    next_action = models.JSONField(null=True, blank=True)
    external_reference = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.INITIATED)

    class Meta:
        unique_together = ("system", "idempotency_key")
        indexes = [
            models.Index(fields=["system", "status"]),
            models.Index(fields=["external_reference"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"INBOUND-{self.id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def amount_remaining(self):
        """Amount authorized but not yet captured."""
        return self.amount_authorized - self.amount_captured

    @property
    def amount_refundable(self):
        """Amount captured but not yet refunded."""
        return self.amount_captured - self.amount_refunded


class InboundPaymentTransaction(BaseModel):
    class Operation(models.TextChoices):
        COLLECTION = "COLLECTION", "Collection"
        AUTHORIZATION = "AUTHORIZATION", "Authorization"
        CAPTURE = "CAPTURE", "Capture"
        REFUND = "REFUND", "Refund"
        PARTIAL_REFUND = "PARTIAL_REFUND", "Partial Refund"
        VOID = "VOID", "Void"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PENDING = "PENDING", "Pending"
        REQUIRES_ACTION = "REQUIRES_ACTION", "Requires Action"
        PROCESSING = "PROCESSING", "Processing"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    TERMINAL_STATUSES = frozenset(
        {
            Status.SUCCESS,
            Status.FAILED,
        }
    )

    inbound_payment = models.ForeignKey(
        InboundPayment,
        on_delete=models.PROTECT,
        related_name="transactions",
    )
    operation_type = models.CharField(max_length=30, choices=Operation.choices)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="inbound_payment_transactions",
    )
    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.PROTECT,
        related_name="inbound_payment_transactions",
    )
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    provider_transaction_id = models.CharField(max_length=255, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)
    provider_payload = models.JSONField(default=dict)
    request_payload = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)
    failure_code = models.CharField(max_length=100, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
    provider_callback_received_at = models.DateTimeField(null=True, blank=True)
    reconciliation_due_at = models.DateTimeField(null=True, blank=True)
    reconciliation_attempts = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["provider_transaction_id"]),
            models.Index(fields=["status"]),
            models.Index(fields=["reconciliation_due_at"]),
        ]

    def __str__(self):
        return f"INBOUND-TRANSACTION-{self.id} [{self.operation_type}:{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


class OutboundRecipientType(models.TextChoices):
    B2C = "B2C", "Business to Customer"
    B2B = "B2B", "Business to Business"


class OutboundTransferEvent(BaseModel):
    RecipientType = OutboundRecipientType

    class CallbackDestination(models.TextChoices):
        SYSTEM = "SYSTEM", "To owning System"
        SOURCE_SYSTEM = (
            "SOURCE_SYSTEM",
            "To the Source System that initiated the transfer",
        )

    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="outbound_transfer_events",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="outbound_transfer_events",
    )
    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.PROTECT,
        related_name="outbound_transfer_events",
    )
    recipient_type = models.CharField(max_length=20, choices=RecipientType.choices)
    payment_method_type = models.ForeignKey(
        PaymentMethodType,
        on_delete=models.PROTECT,
        related_name="outbound_transfer_events",
    )
    currency = models.CharField(max_length=3, default="KES")
    default_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Default reason sent to the provider when the request does not supply one.",
    )
    callback_destination = models.CharField(
        max_length=20,
        choices=CallbackDestination.choices,
        default=CallbackDestination.SYSTEM,
        help_text=(
            "Choose whether transfer status webhooks go to the event owner or the "
            "system that initiated the transfer."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("system", "slug")

    def __str__(self):
        return f"{self.system.slug}/{self.slug}"

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.provider_account_id
            and self.provider_id
            and self.provider_account.provider_id != self.provider_id
        ):
            errors["provider_account"] = "Provider account must belong to the selected provider."
        if (
            self.provider_id
            and self.payment_method_type_id
            and not self.provider.payment_method_types.filter(
                id=self.payment_method_type_id
            ).exists()
        ):
            errors["payment_method_type"] = (
                "Selected provider does not support this payment method."
            )
        if errors:
            raise ValidationError(errors)
        self.currency = self.currency.upper()


class OutboundTransfer(BaseModel):
    RecipientType = OutboundRecipientType

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual Review"

    TERMINAL_STATUSES = frozenset({Status.SUCCESS, Status.FAILED})

    outbound_transfer_event = models.ForeignKey(
        OutboundTransferEvent,
        on_delete=models.PROTECT,
        related_name="outbound_transfers",
    )
    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="outbound_transfers",
    )
    source_system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="initiated_outbound_transfers",
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="outbound_transfers",
    )
    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.PROTECT,
        related_name="outbound_transfers",
    )
    payment_method_type = models.ForeignKey(
        PaymentMethodType,
        on_delete=models.PROTECT,
        related_name="outbound_transfers",
    )
    recipient_type = models.CharField(max_length=20, choices=RecipientType.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    idempotency_key = models.CharField(max_length=255)
    external_reference = models.CharField(max_length=255, blank=True)
    provider_payload = models.JSONField(
        default=dict,
        help_text="Provider-specific recipient and transfer details.",
    )
    provider_transaction_id = models.CharField(max_length=255, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)
    response_payload = models.JSONField(default=dict, blank=True)
    failure_code = models.CharField(max_length=100, blank=True)
    failure_reason = models.TextField(blank=True)
    provider_callback_received_at = models.DateTimeField(null=True, blank=True)
    reconciliation_due_at = models.DateTimeField(null=True, blank=True)
    reconciliation_attempts = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "idempotency_key"],
                name="unique_source_system_outbound_transfer_idempotency",
            )
        ]
        indexes = [
            models.Index(fields=["provider_transaction_id"]),
            models.Index(fields=["status", "reconciliation_due_at"]),
            models.Index(fields=["external_reference"]),
        ]

    def __str__(self):
        return f"OUTBOUND-{self.id} [{self.recipient_type}:{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


class OutboundTransferStateLog(BaseModel):
    outbound_transfer = models.ForeignKey(
        OutboundTransfer,
        on_delete=models.PROTECT,
        related_name="state_logs",
    )
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    reason = models.TextField(blank=True)
    actor = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["created_at"]


class InboundPaymentTransactionStateLog(BaseModel):
    inbound_payment_transaction = models.ForeignKey(
        InboundPaymentTransaction,
        on_delete=models.PROTECT,
        related_name="state_logs",
    )
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    actor = models.CharField(max_length=100, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at"]


class LedgerAccount(BaseModel):
    class AccountType(models.TextChoices):
        ASSET = "ASSET", "Asset"
        LIABILITY = "LIABILITY", "Liability"
        REVENUE = "REVENUE", "Revenue"
        EXPENSE = "EXPENSE", "Expense"
        SUSPENSE = "SUSPENSE", "Suspense"

    code = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=255)
    account_type = models.CharField(max_length=20, choices=AccountType.choices)
    currency = models.CharField(max_length=3)
    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="ledger_accounts",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.code} — {self.name}"


class LedgerPosting(BaseModel):
    class EntryType(models.TextChoices):
        DEBIT = "DEBIT", "Debit"
        CREDIT = "CREDIT", "Credit"

    inbound_payment_transaction = models.ForeignKey(
        InboundPaymentTransaction,
        on_delete=models.PROTECT,
        related_name="postings",
        null=True,
        blank=True,
    )
    outbound_transfer = models.ForeignKey(
        OutboundTransfer,
        on_delete=models.PROTECT,
        related_name="postings",
        null=True,
        blank=True,
    )
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="postings")
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=255, blank=True)
    posting_ref = models.UUIDField()

    class Meta:
        indexes = [models.Index(fields=["posting_ref"])]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        inbound_payment_transaction__isnull=False,
                        outbound_transfer__isnull=True,
                    )
                    | models.Q(
                        inbound_payment_transaction__isnull=True,
                        outbound_transfer__isnull=False,
                    )
                ),
                name="ledger_posting_exactly_one_owner",
            ),
            models.UniqueConstraint(
                fields=["outbound_transfer", "account", "entry_type"],
                condition=models.Q(outbound_transfer__isnull=False),
                name="unique_transfer_account_entry_posting",
            ),
        ]


class WebhookOutbox(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        EXHAUSTED = "EXHAUSTED", "Exhausted"

    system = models.ForeignKey(System, on_delete=models.PROTECT, related_name="webhook_outbox")
    inbound_payment = models.ForeignKey(
        InboundPayment,
        on_delete=models.PROTECT,
        related_name="webhook_outbox",
        null=True,
        blank=True,
    )
    outbound_transfer = models.ForeignKey(
        OutboundTransfer,
        on_delete=models.PROTECT,
        related_name="webhook_outbox",
        null=True,
        blank=True,
    )
    inbound_payment_transaction = models.ForeignKey(
        InboundPaymentTransaction,
        on_delete=models.PROTECT,
        related_name="webhook_outbox",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    destination_url = models.URLField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempt_count = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(inbound_payment__isnull=False, outbound_transfer__isnull=True)
                    | models.Q(inbound_payment__isnull=True, outbound_transfer__isnull=False)
                ),
                name="webhook_outbox_exactly_one_owner",
            )
        ]


class WebhookDeliveryLog(BaseModel):
    outbox = models.ForeignKey(
        WebhookOutbox, on_delete=models.CASCADE, related_name="delivery_logs"
    )
    attempt_number = models.IntegerField()
    request_headers = models.JSONField(default=dict)
    request_payload = models.JSONField()
    response_status_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)


class InboundPaymentCallbackLog(BaseModel):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        REJECTED = "REJECTED", "Rejected"
        IGNORED = "IGNORED", "Ignored"

    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="inbound_payment_callback_logs",
    )
    inbound_payment_transaction = models.ForeignKey(
        InboundPaymentTransaction,
        on_delete=models.PROTECT,
        related_name="callback_logs",
        null=True,
        blank=True,
    )
    raw_headers = models.JSONField(default=dict)
    raw_payload = models.JSONField()
    parsed_status = models.CharField(max_length=50, blank=True)
    processing_error = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
        ]


class OutboundTransferCallbackLog(BaseModel):
    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Received"
        PROCESSING = "PROCESSING", "Processing"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        REJECTED = "REJECTED", "Rejected"
        IGNORED = "IGNORED", "Ignored"

    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="outbound_transfer_callback_logs",
    )
    outbound_transfer = models.ForeignKey(
        OutboundTransfer,
        on_delete=models.PROTECT,
        related_name="callback_logs",
    )
    raw_headers = models.JSONField(default=dict)
    raw_payload = models.JSONField()
    parsed_status = models.CharField(max_length=50, blank=True)
    processing_error = models.TextField(blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
    )

    class Meta:
        indexes = [models.Index(fields=["status", "created_at"])]


class InboundPaymentReconciliation(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        MATCHED = "MATCHED", "Matched"
        MISMATCHED = "MISMATCHED", "Mismatched"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual Review"
        RESOLVED = "RESOLVED", "Resolved"

    inbound_payment_transaction = models.OneToOneField(
        InboundPaymentTransaction,
        on_delete=models.PROTECT,
        related_name="reconciliation",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider_reported_status = models.CharField(max_length=100, blank=True)
    discrepancy_notes = models.TextField(blank=True)
    resolved_by = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
