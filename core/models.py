import hashlib
import re
import secrets
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
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True
    )
    allowed_currencies = models.JSONField(default=list, blank=True)
    rate_limit_per_minute = models.IntegerField(default=60)
    daily_volume_limit = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True
    )
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.hashed_api_key and not re.search(r"^[a-f0-9]{64}$", self.hashed_api_key):
            self.hashed_api_key = hashlib.sha256(
                self.hashed_api_key.encode("utf-8")
            ).hexdigest()
        super().save(*args, **kwargs)

    def verify_api_key(self, raw_key: str) -> bool:
        if not self.hashed_api_key:
            return False
        return self.hashed_api_key == hashlib.sha256(
            raw_key.encode("utf-8")
        ).hexdigest()


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
    payment_method_type = models.ForeignKey(
        PaymentMethodType,
        on_delete=models.PROTECT,
        related_name="providers",
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

    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="accounts"
    )
    name = models.CharField(max_length=255)
    environment = models.CharField(
        max_length=20,
        choices=Environment.choices,
        default=Environment.SANDBOX,
    )
    credentials = models.JSONField()
    extra_config = models.JSONField(default=dict, blank=True)
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


class ChargeableEvent(BaseModel):
    class Flow(models.TextChoices):
        CHARGE = "CHARGE", "Single-step Charge"
        AUTHORIZE_CAPTURE = "AUTHORIZE_CAPTURE", "Authorize & Capture"

    class CallbackDestination(models.TextChoices):
        SYSTEM = "SYSTEM", "To owning System"
        SOURCE_SYSTEM = "SOURCE_SYSTEM", "To the Source System that initiated the payment"

    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="chargeable_events"
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="chargeable_events"
    )
    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.PROTECT,
        related_name="chargeable_events"
    )
    flow = models.CharField(
        max_length=20,
        choices=Flow.choices,
        default=Flow.CHARGE,
        help_text="Payment flow used for this event.",
    )
    auto_capture = models.BooleanField(
        default=False,
        help_text=(
            "AUTHORIZE_CAPTURE flow only. "
            "Automatically capture after a successful authorization."
        ),
    )
    capture_delay_hours = models.IntegerField(
        default=0,
        help_text=(
            "Hours to wait before auto-capturing after authorization. "
            "0 means capture immediately. Only used when auto_capture=True."
        ),
    )
    fixed_amount = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True
    )
    currency = models.CharField(max_length=3, default="KES")
    callback_destination = models.CharField(
        max_length=20,
        choices=CallbackDestination.choices,
        default=CallbackDestination.SYSTEM,
        help_text=mark_safe(
            "<b>Determines which system's webhook URL receives payment webhooks.</b><br><br>"
            "• <b>SYSTEM</b>: Always send to this ChargeableEvent.system.webhook_url.<br>"
            "• <b>SOURCE_SYSTEM</b>: Send to payment_intent.source_system.webhook_url "
            "if it exists. If no source system is set, the webhook will fall back "
            "to the owning ChargeableEvent.system.webhook_url."
        ),
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("system", "slug")

    def __str__(self):
        return f"{self.system.slug}/{self.slug}"


class PaymentMethodToken(BaseModel):
    class TokenType(models.TextChoices):
        CARD = "CARD", "Card"
        MOBILE = "MOBILE", "Mobile Number"
        BANK_ACCOUNT = "BANK_ACCOUNT", "Bank Account"

    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="payment_tokens"
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="payment_tokens"
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


class PaymentIntent(BaseModel):
    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        PENDING = "PENDING", "Pending"
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

    TERMINAL_STATUSES = frozenset({
        Status.SETTLED,
        Status.FAILED,
        Status.CANCELLED,
        Status.REFUNDED,
        Status.EXPIRED,
    })

    # The system that owns this intent
    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="payment_intents"
    )
    # The system that triggered this intent
    source_system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="initiated_payment_intents",
        null=True,
        blank=True,
    )
    chargeable_event = models.ForeignKey(
        ChargeableEvent,
        on_delete=models.PROTECT,
        related_name="payment_intents",
        null=True,
        blank=True,
    )
    payment_method_token = models.ForeignKey(
        PaymentMethodToken,
        on_delete=models.PROTECT,
        related_name="payment_intents",
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
    payment_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider payload supplied at initialization.",
    )
    next_action = models.JSONField(null=True, blank=True)
    external_reference = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.INITIATED
    )

    class Meta:
        unique_together = ("system", "idempotency_key")
        indexes = [
            models.Index(fields=["system", "status"]),
            models.Index(fields=["external_reference"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"PI-{self.id} [{self.status}]"

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


class Transaction(BaseModel):
    class Type(models.TextChoices):
        PAYMENT = "PAYMENT", "Payment"
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

    TERMINAL_STATUSES = frozenset({
        Status.SUCCESS,
        Status.FAILED,
    })

    payment_intent = models.ForeignKey(
        PaymentIntent,
        on_delete=models.PROTECT,
        related_name="transactions"
    )
    transaction_type = models.CharField(max_length=30, choices=Type.choices)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="transactions"
    )
    provider_account = models.ForeignKey(
        ProviderAccount,
        on_delete=models.PROTECT,
        related_name="transactions"
    )
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    provider_transaction_id = models.CharField(max_length=255, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)
    request_payload = models.JSONField(default=dict)
    response_payload = models.JSONField(default=dict, blank=True)
    failure_reason = models.TextField(blank=True)
    failure_code = models.CharField(max_length=100, blank=True)
    celery_task_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.PENDING
    )
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
        return f"TXN-{self.id} [{self.transaction_type}:{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES


class TransactionStateLog(BaseModel):
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="state_logs"
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

    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="postings"
    )
    account = models.ForeignKey(
        LedgerAccount,
        on_delete=models.PROTECT,
        related_name="postings"
    )
    entry_type = models.CharField(max_length=10, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    currency = models.CharField(max_length=3)
    description = models.CharField(max_length=255, blank=True)
    posting_ref = models.UUIDField()

    class Meta:
        indexes = [models.Index(fields=["posting_ref"])]


class WebhookOutbox(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        EXHAUSTED = "EXHAUSTED", "Exhausted"

    system = models.ForeignKey(
        System,
        on_delete=models.PROTECT,
        related_name="webhook_outbox"
    )
    payment_intent = models.ForeignKey(
        PaymentIntent,
        on_delete=models.PROTECT,
        related_name="webhook_outbox"
    )
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.PROTECT,
        related_name="webhook_outbox",
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=100)
    payload = models.JSONField()
    destination_url = models.URLField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    attempt_count = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=5)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_attempted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "next_attempt_at"]),
        ]


class WebhookDeliveryLog(BaseModel):
    outbox = models.ForeignKey(
        WebhookOutbox,
        on_delete=models.CASCADE,
        related_name="delivery_logs"
    )
    attempt_number = models.IntegerField()
    request_headers = models.JSONField(default=dict)
    request_payload = models.JSONField()
    response_status_code = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    response_headers = models.JSONField(default=dict, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)


class ProviderCallbackLog(BaseModel):
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
        related_name="callback_logs"
    )
    transaction = models.ForeignKey(
        Transaction,
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


class ReconciliationRecord(BaseModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        MATCHED = "MATCHED", "Matched"
        MISMATCHED = "MISMATCHED", "Mismatched"
        MANUAL_REVIEW = "MANUAL_REVIEW", "Manual Review"
        RESOLVED = "RESOLVED", "Resolved"

    transaction = models.OneToOneField(
        Transaction,
        on_delete=models.PROTECT,
        related_name="reconciliation"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )
    provider_reported_status = models.CharField(max_length=100, blank=True)
    discrepancy_notes = models.TextField(blank=True)
    resolved_by = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.IntegerField(default=0)
    last_attempted_at = models.DateTimeField(null=True, blank=True)
