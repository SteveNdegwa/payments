from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from django.urls import reverse

from .models import (
    System, PaymentMethodType, Provider, ProviderAccount,
    ChargeableEvent, PaymentMethodToken, PaymentIntent,
    Transaction, TransactionStateLog, LedgerAccount, LedgerPosting,
    WebhookOutbox, WebhookDeliveryLog, ProviderCallbackLog,
    ReconciliationRecord
)


class TransactionInline(admin.TabularInline):
    model = Transaction
    extra = 0
    max_num = 8
    can_delete = False
    readonly_fields = [
        'transaction_type',
        'status_colored',
        'amount_currency',
        'provider',
        'provider_transaction_id',
        'created_at_relative',
    ]
    fields = [
        'transaction_type',
        'status_colored',
        'amount_currency',
        'provider',
        'provider_transaction_id',
        'created_at_relative',
    ]
    ordering = ['-created_at']
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'SUCCESS': '#28a745',
            'FAILED': '#dc3545',
            'PENDING': '#fd7e14',
            'PROCESSING': '#6c757d',
            'QUEUED': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:500">{}</span>', color, obj.status)

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.days < 1:
            hours = int(delta.total_seconds() // 3600)
            return f"{hours}h ago" if hours else "recent"
        return f"{delta.days}d ago"


class PaymentIntentInline(admin.TabularInline):
    model = PaymentIntent
    extra = 0
    max_num = 6
    can_delete = False
    readonly_fields = [
        'status_colored',
        'amount_currency',
        'idempotency_key_short',
        'created_at_relative',
    ]
    fields = [
        'status_colored',
        'amount_currency',
        'idempotency_key_short',
        'created_at_relative',
    ]
    ordering = ['-created_at']
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'CAPTURED': '#28a745',
            'AUTHORIZED': '#17a2b8',
            'SETTLED': '#28a745',
            'FAILED': '#dc3545',
            'CANCELLED': '#6c757d',
            'REFUNDED': '#6f42c1',
            'EXPIRED': '#6c757d',
            'PENDING': '#fd7e14',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:500">{}</span>', color, obj.status)

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Idempotency')
    def idempotency_key_short(self, obj):
        return obj.idempotency_key[:16] + '…' if len(obj.idempotency_key) > 16 else obj.idempotency_key

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.days < 1:
            return "<1d"
        return f"{delta.days}d ago"


class WebhookDeliveryLogInline(admin.TabularInline):
    model = WebhookDeliveryLog
    extra = 0
    max_num = 10
    can_delete = False
    fields = [
        'attempt_number',
        'response_status_code_colored',
        'duration_ms',
        'error_short',
        'created_at',
    ]
    readonly_fields = [
        'attempt_number',
        'response_status_code_colored',
        'duration_ms',
        'error_short',
        'created_at',
    ]
    ordering = ['attempt_number']

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def response_status_code_colored(self, obj):
        code = obj.response_status_code
        if code is None:
            return "—"
        if 200 <= code < 300:
            return format_html('<span style="color:#28a745">{}</span>', code)
        elif 400 <= code < 500:
            return format_html('<span style="color:#dc3545">{}</span>', code)
        else:
            return format_html('<span style="color:#fd7e14">{}</span>', code)

    @admin.display(description='Error')
    def error_short(self, obj):
        if not obj.error_message:
            return "—"
        return obj.error_message[:60] + '…' if len(obj.error_message) > 60 else obj.error_message


@admin.register(System)
class SystemAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'slug',
        'is_active_colored',
        'rate_limit_per_minute',
        'max_transaction_amount',
        'daily_volume_limit',
        'allowed_currencies_short',
        'created_at_relative',
    ]
    list_filter = ['is_active', 'allowed_currencies']
    search_fields = ['name', 'slug']
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'hashed_api_key',
    ]

    fieldsets = (
        ('Identity', {
            'fields': (
                'name',
                'slug',
                'is_active',
            ),
        }),
        ('Security', {
            'fields': (
                'hashed_api_key',
                'allowed_ips',
            ),
            'classes': ('wide',),
        }),
        ('Limits', {
            'fields': (
                'rate_limit_per_minute',
                'max_transaction_amount',
                'daily_volume_limit',
                'allowed_currencies',
            ),
        }),
        ('Webhook', {
            'fields': (
                'webhook_url',
                'webhook_secret',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745">Yes</span>')
        return format_html('<span style="color:#dc3545">No</span>')

    @admin.display(description='Currencies')
    def allowed_currencies_short(self, obj):
        if not obj.allowed_currencies:
            return "Any"
        joined = ", ".join(obj.allowed_currencies)
        return joined[:40] + "…" if len(joined) > 40 else joined

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        return "today" if delta.days == 0 else f"{delta.days}d ago"


@admin.register(PaymentMethodType)
class PaymentMethodTypeAdmin(admin.ModelAdmin):
    list_display = [
        'code',
        'name',
        'is_active_colored',
    ]
    list_filter = ['is_active']
    search_fields = ['code', 'name']
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        (None, {
            'fields': (
                'code',
                'name',
                'is_active',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        color = '#28a745' if obj.is_active else '#dc3545'
        text = "Yes" if obj.is_active else "No"
        return format_html('<span style="color:{}">{}</span>', color, text)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    list_display = [
        'name',
        'slug',
        'payment_method_type',
        'is_active_colored',
        'is_async_colored',
        'supports_refund_colored',
        'supports_3ds_colored',
    ]
    list_filter = [
        'is_active',
        'is_async',
        'payment_method_type',
        'supports_refund',
        'supports_3ds',
    ]
    search_fields = ['name', 'slug', 'class_name']
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        ('Identity', {
            'fields': (
                'name',
                'slug',
                'class_name',
                'payment_method_type',
            ),
        }),
        ('Capabilities', {
            'fields': (
                ('is_async', 'supports_refund', 'supports_partial_refund'),
                ('supports_authorize_capture', 'supports_3ds'),
            ),
        }),
        ('Reconciliation', {
            'fields': (
                'reconciliation_timeout_seconds',
            ),
        }),
        ('Status', {
            'fields': (
                'is_active',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745">Yes</span>')
        return format_html('<span style="color:#dc3545">No</span>')

    @admin.display(description='Async')
    def is_async_colored(self, obj):
        if obj.is_async:
            return format_html('<span style="color:#6c757d">Yes</span>')
        return "No"

    @admin.display(description='Refund')
    def supports_refund_colored(self, obj):
        if obj.supports_refund:
            return format_html('<span style="color:#28a745">Yes</span>')
        return "—"

    @admin.display(description='3DS')
    def supports_3ds_colored(self, obj):
        if obj.supports_3ds:
            return format_html('<span style="color:#17a2b8">Yes</span>')
        return "—"


@admin.register(ProviderAccount)
class ProviderAccountAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'environment',
        'is_default_colored',
        'is_active_colored',
        'created_at_relative',
    ]
    list_filter = [
        'environment',
        'is_default',
        'is_active',
        'provider',
    ]
    search_fields = ['name', 'provider__name']
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        (None, {
            'fields': (
                'provider',
                'name',
                'environment',
                'is_default',
                'is_active',
            ),
        }),
        ('Configuration', {
            'fields': (
                'credentials',
                'extra_config',
            ),
            'classes': ('wide',),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Default')
    def is_default_colored(self, obj):
        if obj.is_default:
            return format_html('<span style="color:#28a745">Yes</span>')
        return "—"

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745">Yes</span>')
        return format_html('<span style="color:#dc3545">No</span>')

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        return f"{delta.days}d ago" if delta.days > 0 else "today"


@admin.register(ChargeableEvent)
class ChargeableEventAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'system',
        'provider',
        'flow',
        'currency',
        'fixed_amount',
        'is_active_colored',
    ]
    list_filter = [
        'system',
        'provider',
        'flow',
        'currency',
        'is_active',
    ]
    search_fields = [
        'name',
        'slug',
        'system__slug',
        'provider__name',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        ('Identity', {
            'fields': (
                'system',
                'name',
                'slug',
                'description',
            ),
        }),
        ('Processing', {
            'fields': (
                'provider',
                'provider_account',
                'flow',
                'auto_capture',
                'capture_delay_hours',
            ),
        }),
        ('Amount', {
            'fields': (
                'fixed_amount',
                'currency',
            ),
        }),
        ('Status', {
            'fields': (
                'is_active',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745">Yes</span>')
        return format_html('<span style="color:#dc3545">No</span>')


@admin.register(PaymentMethodToken)
class PaymentMethodTokenAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'system',
        'provider',
        'token_type',
        'card_brand',
        'expiry',
        'is_active_colored',
    ]
    list_filter = [
        'system',
        'provider',
        'token_type',
        'is_active',
    ]
    search_fields = [
        'masked_identifier',
        'customer_ref',
        'system__slug',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        ('Token', {
            'fields': (
                'system',
                'provider',
                'token_type',
                'provider_token',
            ),
        }),
        ('Details', {
            'fields': (
                'masked_identifier',
                'card_brand',
                ('expiry_month', 'expiry_year'),
                'customer_ref',
            ),
        }),
        ('Status', {
            'fields': (
                'is_active',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Expiry')
    def expiry(self, obj):
        if obj.expiry_month and obj.expiry_year:
            return f"{obj.expiry_month}/{obj.expiry_year}"
        return "—"

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color:#28a745">Yes</span>')
        return format_html('<span style="color:#dc3545">No</span>')


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'system',
        'status_colored',
        'amount_currency',
        'chargeable_event',
        'payment_method_token_short',
        'created_at_relative',
    ]
    list_filter = [
        'system',
        'status',
        'currency',
        'chargeable_event',
        'created_at',
    ]
    search_fields = [
        'idempotency_key',
        'external_reference',
        'system__slug',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'amount_authorized',
        'amount_captured',
        'amount_refunded',
        'amount_settled',
    ]
    inlines = [TransactionInline]
    date_hierarchy = 'created_at'

    fieldsets = (
        ('Core', {
            'fields': (
                ('system', 'source_system'),
                'chargeable_event',
                'payment_method_token',
                ('amount', 'currency'),
            ),
        }),
        ('Amounts', {
            'fields': (
                ('amount_authorized', 'amount_captured'),
                ('amount_refunded', 'amount_settled'),
            ),
            'classes': ('wide',),
        }),
        ('State', {
            'fields': (
                'status',
                'next_action',
                'expires_at',
            ),
        }),
        ('References', {
            'fields': (
                'idempotency_key',
                'external_reference',
                'payment_payload',
            ),
            'classes': ('wide',),
        }),
        ('Metadata', {
            'fields': (
                'metadata',
            ),
            'classes': ('collapse',),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'CAPTURED': '#28a745',
            'SETTLED': '#28a745',
            'AUTHORIZED': '#17a2b8',
            'FAILED': '#dc3545',
            'CANCELLED': '#dc3545',
            'EXPIRED': '#dc3545',
            'REFUNDED': '#6f42c1',
            'PENDING': '#fd7e14',
            'PROCESSING': '#fd7e14',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:600">{}</span>', color, obj.status)

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Token')
    def payment_method_token_short(self, obj):
        if not obj.payment_method_token:
            return "—"
        return str(obj.payment_method_token)[:24] + "…"

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        return "today" if delta.days == 0 else f"{delta.days}d ago"


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'payment_intent_link',
        'transaction_type',
        'status_colored',
        'amount_currency',
        'provider',
        'created_at_relative',
    ]
    list_filter = [
        'transaction_type',
        'status',
        'provider',
        'currency',
        'created_at',
    ]
    search_fields = [
        'provider_transaction_id',
        'provider_reference',
        'payment_intent__idempotency_key',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'celery_task_id',
    ]
    # No WebhookDeliveryLogInline here - it doesn't belong (fixed E202)
    date_hierarchy = 'created_at'

    fieldsets = (
        ('Core', {
            'fields': (
                'payment_intent',
                'transaction_type',
                'status',
            ),
        }),
        ('Provider', {
            'fields': (
                'provider',
                'provider_account',
                'provider_transaction_id',
                'provider_reference',
            ),
        }),
        ('Amount', {
            'fields': (
                ('amount', 'currency'),
            ),
        }),
        ('Payloads', {
            'fields': (
                'request_payload',
                'response_payload',
            ),
            'classes': ('wide', 'collapse'),
        }),
        ('Failure', {
            'fields': (
                'failure_reason',
                'failure_code',
            ),
            'classes': ('collapse',),
        }),
        ('Timing', {
            'fields': (
                'provider_callback_received_at',
                'reconciliation_due_at',
                'reconciliation_attempts',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
                'celery_task_id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Intent')
    def payment_intent_link(self, obj):
        url = reverse("admin:core_paymentintent_change", args=[obj.payment_intent.id])
        return format_html('<a href="{}">{}</a>', url, f"PI-{obj.payment_intent.id}")

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'SUCCESS': '#28a745',
            'FAILED': '#dc3545',
            'PENDING': '#fd7e14',
            'PROCESSING': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:600">{}</span>', color, obj.status)

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        return f"{delta.days}d ago" if delta.days > 0 else "today"


@admin.register(TransactionStateLog)
class TransactionStateLogAdmin(admin.ModelAdmin):
    list_display = [
        'transaction',
        'from_status',
        'to_status',
        'created_at',
        'actor',
    ]
    list_filter = [
        'transaction__payment_intent__system',
        'created_at',
    ]
    search_fields = [
        'transaction__id',
        'reason',
        'actor',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    date_hierarchy = 'created_at'

    fieldsets = (
        (None, {
            'fields': (
                'transaction',
                'from_status',
                'to_status',
                'reason',
                'actor',
                'metadata',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    def has_add_permission(self, request):
        return False


@admin.register(LedgerAccount)
class LedgerAccountAdmin(admin.ModelAdmin):
    list_display = [
        'code',
        'name',
        'account_type',
        'currency',
        'system',
        'is_active_colored',
    ]
    list_filter = [
        'account_type',
        'currency',
        'system',
        'is_active',
    ]
    search_fields = ['code', 'name']
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]

    fieldsets = (
        (None, {
            'fields': (
                'code',
                'name',
                'account_type',
                'currency',
                'system',
                'is_active',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        color = '#28a745' if obj.is_active else '#dc3545'
        return format_html('<span style="color:{}">{}</span>', color, "Yes" if obj.is_active else "No")


@admin.register(LedgerPosting)
class LedgerPostingAdmin(admin.ModelAdmin):
    list_display = [
        'transaction_link',
        'account',
        'entry_type_colored',
        'amount_currency',
        'posting_ref_short',
        'created_at_relative',
    ]
    list_filter = [
        'entry_type',
        'currency',
        'account__account_type',
        'created_at',
    ]
    search_fields = [
        'transaction__id',
        'account__code',
        'description',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    fieldsets = (
        (None, {
            'fields': (
                'transaction',
                'account',
                'entry_type',
                'amount',
                'currency',
                'description',
                'posting_ref',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Transaction')
    def transaction_link(self, obj):
        url = reverse("admin:core_transaction_change", args=[obj.transaction.id])
        return format_html('<a href="{}">{}</a>', url, f"TXN-{obj.transaction.id}")

    @admin.display(description='Type')
    def entry_type_colored(self, obj):
        color = '#28a745' if obj.entry_type == 'CREDIT' else '#dc3545'
        return format_html('<span style="color:{}">{}</span>', color, obj.entry_type)

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Ref')
    def posting_ref_short(self, obj):
        return str(obj.posting_ref)[:8] + "…"

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        return f"{delta.days}d ago" if delta.days > 0 else "today"


@admin.register(WebhookOutbox)
class WebhookOutboxAdmin(admin.ModelAdmin):
    list_display = [
        'system',
        'event_type',
        'status_colored',
        'attempt_count_display',
        'next_attempt_at',
        'payment_intent_short',
        'created_at_relative',
    ]
    list_filter = [
        'system',
        'status',
        'event_type',
        'next_attempt_at',
    ]
    search_fields = [
        'payment_intent__idempotency_key',
        'event_type',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    inlines = [WebhookDeliveryLogInline]
    date_hierarchy = 'next_attempt_at'

    fieldsets = (
        ('Target', {
            'fields': (
                'system',
                'destination_url',
            ),
        }),
        ('Event', {
            'fields': (
                'payment_intent',
                'transaction',
                'event_type',
                'payload',
            ),
        }),
        ('Delivery', {
            'fields': (
                'status',
                'attempt_count',
                'max_attempts',
                'next_attempt_at',
                'last_attempted_at',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'DELIVERED': '#28a745',
            'FAILED': '#dc3545',
            'EXHAUSTED': '#dc3545',
            'PROCESSING': '#fd7e14',
            'PENDING': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:500">{}</span>', color, obj.status)

    @admin.display(description='Attempts')
    def attempt_count_display(self, obj):
        if obj.attempt_count == 0:
            return "—"
        color = '#dc3545' if obj.status in ['FAILED', 'EXHAUSTED'] else '#6c757d'
        return format_html(
            '<span style="color:{}">{}/{}<span>',
            color, obj.attempt_count, obj.max_attempts
        )

    @admin.display(description='Intent')
    def payment_intent_short(self, obj):
        if not obj.payment_intent:
            return "—"
        return f"PI-{obj.payment_intent.id}"

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() // 60)
            return f"{mins} min ago" if mins else "just now"
        elif delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        else:
            return f"{delta.days}d ago"


@admin.register(WebhookDeliveryLog)
class WebhookDeliveryLogAdmin(admin.ModelAdmin):
    list_display = [
        'outbox_link',
        'attempt_number',
        'response_status_colored',
        'duration_display',
        'created_at_relative',
        'error_short',
    ]
    list_filter = [
        'outbox__system',
        'outbox__event_type',
        'response_status_code',
        'outbox__status',
        'created_at',
    ]
    search_fields = [
        'outbox__payment_intent__idempotency_key',
        'outbox__event_type',
        'error_message',
        'response_body',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'outbox',
        'attempt_number',
        'request_headers',
        'request_payload',
        'response_status_code',
        'response_body',
        'response_headers',
        'duration_ms',
        'error_message',
    ]
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    list_per_page = 40

    fieldsets = (
        ('Webhook Outbox', {
            'fields': (
                'outbox',
                'attempt_number',
            ),
        }),
        ('Request', {
            'fields': (
                'request_headers',
                'request_payload',
            ),
            'classes': ('wide', 'collapse'),
        }),
        ('Response', {
            'fields': (
                'response_status_code',
                'response_body',
                'response_headers',
                'duration_ms',
            ),
            'classes': ('wide',),
        }),
        ('Error', {
            'fields': (
                'error_message',
            ),
            'classes': ('collapse',),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Outbox / Event')
    def outbox_link(self, obj):
        if not obj.outbox:
            return "—"
        url = reverse("admin:core_webhookoutbox_change", args=[obj.outbox.id])
        event = obj.outbox.event_type[:28] + "…" if len(obj.outbox.event_type) > 28 else obj.outbox.event_type
        return format_html(
            '<a href="{}">{} — {}</a>',
            url,
            f"Outbox #{obj.outbox.id}",
            event
        )

    @admin.display(description='Status')
    def response_status_colored(self, obj):
        code = obj.response_status_code
        if code is None:
            return format_html('<span style="color:#6c757d">—</span>')
        if 200 <= code < 300:
            return format_html('<span style="color:#28a745;font-weight:500">{}</span>', code)
        elif 400 <= code < 500:
            return format_html('<span style="color:#dc3545;font-weight:500">{}</span>', code)
        else:
            return format_html('<span style="color:#fd7e14;font-weight:500">{}</span>', code)

    @admin.display(description='Duration')
    def duration_display(self, obj):
        if obj.duration_ms is None:
            return "—"
        if obj.duration_ms < 1000:
            return f"{obj.duration_ms} ms"
        sec = obj.duration_ms / 1000
        return f"{sec:.2f} s"

    @admin.display(description='Error')
    def error_short(self, obj):
        if not obj.error_message:
            return "—"
        return obj.error_message[:60] + "…" if len(obj.error_message) > 60 else obj.error_message

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() // 60)
            return f"{mins} min ago" if mins else "just now"
        elif delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        else:
            return f"{delta.days}d ago"


@admin.register(ProviderCallbackLog)
class ProviderCallbackLogAdmin(admin.ModelAdmin):
    list_display = [
        'provider',
        'parsed_status',
        'processed_colored',
        'transaction',
        'created_at_relative',
    ]
    list_filter = [
        'provider',
        'processed',
        'created_at',
    ]
    search_fields = [
        'raw_payload',
        'processing_error',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    fieldsets = (
        ('Provider', {
            'fields': (
                'provider',
                'transaction',
            ),
        }),
        ('Payload', {
            'fields': (
                'raw_headers',
                'raw_payload',
            ),
            'classes': ('wide',),
        }),
        ('Result', {
            'fields': (
                'parsed_status',
                'processed',
                'processing_error',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Processed')
    def processed_colored(self, obj):
        if obj.processed:
            return format_html('<span style="color:#28a745">Yes</span>')
        if obj.processing_error:
            return format_html('<span style="color:#dc3545">Error</span>')
        return format_html('<span style="color:#fd7e14">No</span>')

    @admin.display(description='Created')
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() // 60)
            return f"{mins} min ago" if mins else "just now"
        elif delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        else:
            return f"{delta.days}d ago"


@admin.register(ReconciliationRecord)
class ReconciliationRecordAdmin(admin.ModelAdmin):
    list_display = [
        'transaction_link',
        'status_colored',
        'provider_reported_status',
        'attempts',
        'last_attempted_at',
    ]
    list_filter = [
        'status',
        'created_at',
    ]
    search_fields = [
        'transaction__id',
        'discrepancy_notes',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    fieldsets = (
        ('Transaction', {
            'fields': (
                'transaction',
            ),
        }),
        ('Status', {
            'fields': (
                'status',
                'provider_reported_status',
            ),
        }),
        ('Resolution', {
            'fields': (
                'discrepancy_notes',
                'resolved_by',
                'resolved_at',
            ),
        }),
        ('Attempts', {
            'fields': (
                'attempts',
                'last_attempted_at',
            ),
        }),
        ('Audit', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'MATCHED': '#28a745',
            'RESOLVED': '#28a745',
            'MISMATCHED': '#dc3545',
            'MANUAL_REVIEW': '#fd7e14',
            'PENDING': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html('<span style="color:{};font-weight:600">{}</span>', color, obj.status)

    @admin.display(description='Transaction')
    def transaction_link(self, obj):
        url = reverse("admin:core_transaction_change", args=[obj.transaction.id])
        return format_html('<a href="{}">{}</a>', url, f"TXN-{obj.transaction.id}")