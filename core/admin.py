import hashlib

from django.contrib import admin, messages
from django.utils.html import format_html
from django.utils import timezone
from django.urls import reverse

from .models import (
    System, PaymentMethodType, Provider, ProviderAccount,
    ChargeableEvent, PaymentMethodToken, PaymentIntent,
    Transaction, TransactionStateLog, LedgerAccount, LedgerPosting,
    WebhookOutbox, WebhookDeliveryLog, ProviderCallbackLog,
    ReconciliationRecord, generate_api_key
)


def format_datetime_admin(dt):
    if dt is None:
        return "—"

    dt_local = timezone.localtime(dt)
    now = timezone.localtime(timezone.now())
    delta = now - dt_local
    time_str = dt_local.strftime("%H:%M")

    if delta.days == 0:
        return f"today {time_str}"
    if delta.days == 1:
        return f"yesterday {time_str}"
    if delta.days <= 3:
        return f"{delta.days}d ago {time_str}"
    if delta.days <= 14:
        return dt_local.strftime("%a %H:%M")
    if delta.days <= 62:
        return dt_local.strftime("%b %d %H:%M")
    if now.year == dt_local.year:
        return dt_local.strftime("%b %d")
    return dt_local.strftime("%Y-%m-%d")


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
        'created_at_display',
    ]
    fields = [
        'transaction_type',
        'status_colored',
        'amount_currency',
        'provider',
        'provider_transaction_id',
        'created_at_display',
    ]
    ordering = ['-created_at']
    show_change_link = True
    classes = ('collapse',)

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

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


class PaymentIntentInline(admin.TabularInline):
    model = PaymentIntent
    extra = 0
    max_num = 6
    can_delete = False
    readonly_fields = [
        'status_colored',
        'amount_currency',
        'idempotency_key_short',
        'created_at_display',
    ]
    fields = readonly_fields
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
        return format_html(
            '<span style="color:{};font-weight:500">{}</span>',
            colors.get(obj.status, '#6c757d'), obj.status
        )

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Idempotency')
    def idempotency_key_short(self, obj):
        key = obj.idempotency_key or ""
        return (key[:16] + '…') if len(key) > 16 else key

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


class TransactionStateLogInline(admin.TabularInline):
    model = TransactionStateLog
    extra = 0
    can_delete = False
    fields = ['from_status', 'to_status', 'reason', 'actor', 'created_at_display']
    readonly_fields = fields
    ordering = ['-created_at']
    verbose_name = "State Change"
    verbose_name_plural = "State History"
    classes = ('collapse',)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='When', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


class WebhookOutboxInline(admin.TabularInline):
    model = WebhookOutbox
    extra = 0
    max_num = 12
    can_delete = False
    readonly_fields = [
        'event_type',
        'status_colored',
        'attempt_count_display',
        'next_attempt_at',
        'last_attempted_at',
        'created_at_display',
    ]
    fields = [
        'event_type',
        'status_colored',
        'attempt_count_display',
        'next_attempt_at',
        'last_attempted_at',
        'created_at_display',
    ]
    ordering = ['-created_at']
    show_change_link = True
    classes = ('collapse',)

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'DELIVERED': '#28a745',
            'FAILED': '#dc3545',
            'EXHAUSTED': '#dc3545',
            'PROCESSING': '#fd7a14',
            'PENDING': '#6c757d',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html(
            '<span style="color:{}; font-weight:500">{}</span>',
            color, obj.status
        )

    @admin.display(description='Attempts')
    def attempt_count_display(self, obj):
        if obj.attempt_count == 0:
            return "—"
        color = '#dc3545' if obj.status in ['FAILED', 'EXHAUSTED'] else '#6c757d'
        return format_html(
            '<span style="color:{}">{}/{}</span>',
            color, obj.attempt_count, obj.max_attempts
        )

    @admin.display(description='Next attempt')
    def next_attempt_at(self, obj):
        if not obj.next_attempt_at:
            return "—"
        return format_datetime_admin(obj.next_attempt_at)

    @admin.display(description='Last attempted')
    def last_attempted_at(self, obj):
        return format_datetime_admin(obj.last_attempted_at)

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


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
        'created_at_display',
    ]
    readonly_fields = fields
    ordering = ['attempt_number']
    classes = ('collapse',)

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='Status')
    def response_status_code_colored(self, obj):
        code = obj.response_status_code
        if code is None:
            return "—"
        if 200 <= code < 300:
            color = '#28a745'
        elif 400 <= code < 500:
            color = '#dc3545'
        else:
            color = '#fd7e14'
        return format_html('<span style="color:{}">{}</span>', color, code)

    @admin.display(description='Error')
    def error_short(self, obj):
        if not obj.error_message:
            return "—"
        msg = obj.error_message
        return (msg[:60] + '…') if len(msg) > 60 else msg

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


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
        'created_at_display',
    ]
    list_filter = ['is_active']
    search_fields = ['name', 'slug']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id', 'hashed_api_key']

    fieldsets = (
        ('Identity', {'fields': ('name', 'slug', 'is_active')}),
        ('Security', {
            'fields': ('hashed_api_key', 'allowed_ips'),
            'classes': ('wide',),
            'description': format_html(
                'API key is auto-generated on creation and shown <strong>only once</strong> after save.'
            )
        }),
        ('Limits', {'fields': (
            'rate_limit_per_minute', 'max_transaction_amount', 'daily_volume_limit', 'allowed_currencies'
        )}),
        ('Webhook', {'fields': ('webhook_url', 'webhook_secret')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    actions = ['reset_api_key']

    def save_model(self, request, obj, form, change):
        is_new = not change

        if is_new:
            plain_key = generate_api_key()
            obj.hashed_api_key = hashlib.sha256(plain_key.encode("utf-8")).hexdigest()

            messages.success(
                request,
                format_html(
                    '<div class="alert alert-info alert-dismissible fade show" role="alert" '
                    'style="margin: 1rem 0; padding: 0.75rem 1.25rem; border-radius: 0.375rem; '
                    'background-color: var(--bs-info-bg, #cff4fc); '
                    'border-color: var(--bs-info-border-subtle, #b6effb); '
                    'color: var(--bs-info-text, #055160); font-size: 0.95rem;">'

                    '<strong>API key generated</strong><br>'
                    'New key for <strong>{}</strong>:<br>'
                    '<code style="font-size: 1.15rem; padding: 0.4rem 0.6rem; background: white; '
                    'border: 1px solid #ced4da; border-radius: 0.25rem; font-family: monospace; '
                    'display: inline-block; margin: 0.5rem 0;">'
                    '{}</code><br>'
                    '<small style="opacity: 0.9;">'
                    'Copy and store it securely now — it will not be shown again. '
                    'Reset anytime via Actions → Reset API Key.'
                    '</small>'
                    '</div>',
                    obj.name,
                    plain_key
                ),
                extra_tags='safe html'
            )

        super().save_model(request, obj, form, change)

    def reset_api_key(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                "Please select exactly one System to reset the API key.",
                level=messages.ERROR
            )
            return

        obj = queryset.first()

        plain_key = generate_api_key()
        obj.hashed_api_key = hashlib.sha256(plain_key.encode("utf-8")).hexdigest()
        obj.save(update_fields=['hashed_api_key'])

        self.message_user(
            request,
            format_html(
                '<div class="alert alert-info alert-dismissible fade show" role="alert" '
                'style="margin: 1rem 0; padding: 0.75rem 1.25rem; border-radius: 0.375rem; '
                'background-color: var(--bs-info-bg, #cff4fc); '
                'border-color: var(--bs-info-border-subtle, #b6effb); '
                'color: var(--bs-info-text, #055160); font-size: 0.95rem;">'

                '<strong>API key reset</strong><br>'
                'New key for <strong>{}</strong>:<br>'
                '<code style="font-size: 1.15rem; padding: 0.4rem 0.6rem; background: white; '
                'border: 1px solid #ced4da; border-radius: 0.25rem; font-family: monospace; '
                'display: inline-block; margin: 0.5rem 0;">'
                '{}</code><br>'
                '<small style="opacity: 0.9;">'
                'Copy and store it securely now — it will not be shown again. '
                'Previous key is now invalid. Update the client.'
                '</small>'
                '</div>',
                obj.name,
                plain_key
            ),
            level=messages.SUCCESS,
            extra_tags='safe html'
        )

    reset_api_key.short_description = "Reset API Key"

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        return format_html(
            '<span style="color:#28a745">Yes</span>' if obj.is_active else '<span style="color:#dc3545">No</span>'
        )

    @admin.display(description='Currencies')
    def allowed_currencies_short(self, obj):
        if not obj.allowed_currencies:
            return "Any"
        joined = ", ".join(obj.allowed_currencies)
        return joined[:40] + "…" if len(joined) > 40 else joined

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(PaymentMethodType)
class PaymentMethodTypeAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'is_active_colored']
    list_filter = ['is_active']
    search_fields = ['code', 'name']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']

    fieldsets = (
        (None, {'fields': ('code', 'name', 'is_active')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        color = '#28a745' if obj.is_active else '#dc3545'
        return format_html('<span style="color:{}">{}</span>', color, "Yes" if obj.is_active else "No")


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
    list_filter = ['is_active', 'is_async', 'payment_method_type', 'supports_refund', 'supports_3ds']
    search_fields = ['name', 'slug', 'class_name']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    ordering = ['name']

    fieldsets = (
        ('Identity', {'fields': ('name', 'slug', 'class_name', 'payment_method_type')}),
        ('Capabilities', {'fields': (('is_async', 'supports_refund', 'supports_partial_refund'), ('supports_authorize_capture', 'supports_3ds'))}),
        ('Reconciliation', {'fields': ('reconciliation_timeout_seconds',)}),
        ('Status', {'fields': ('is_active',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        return format_html('<span style="color:#28a745">Yes</span>' if obj.is_active else '<span style="color:#dc3545">No</span>')

    @admin.display(description='Async')
    def is_async_colored(self, obj):
        return format_html('<span style="color:#6c757d">Yes</span>' if obj.is_async else "No")

    @admin.display(description='Refund')
    def supports_refund_colored(self, obj):
        return format_html('<span style="color:#28a745">Yes</span>') if obj.supports_refund else "—"

    @admin.display(description='3DS')
    def supports_3ds_colored(self, obj):
        return format_html('<span style="color:#17a2b8">Yes</span>') if obj.supports_3ds else "—"


@admin.register(ProviderAccount)
class ProviderAccountAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'environment', 'is_default_colored', 'is_active_colored', 'created_at_display']
    list_filter = ['environment', 'is_default', 'is_active', 'provider']
    search_fields = ['name', 'provider__name']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    ordering = ['-is_default', 'provider__name', 'name']

    fieldsets = (
        (None, {'fields': ('provider', 'name', 'environment', 'is_default', 'is_active')}),
        ('Configuration', {'fields': ('credentials', 'extra_config'), 'classes': ('wide',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Default')
    def is_default_colored(self, obj):
        return format_html('<span style="color:#28a745">Yes</span>') if obj.is_default else "—"

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        color = '#28a745' if obj.is_active else '#dc3545'
        return format_html('<span style="color:{}">{}</span>', color, "Yes" if obj.is_active else "No")

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(ChargeableEvent)
class ChargeableEventAdmin(admin.ModelAdmin):
    list_display = [
        '__str__', 'system', 'provider', 'flow', 'currency', 'fixed_amount',
        'callback_destination', 'is_active_colored'
    ]
    list_filter = ['system', 'provider', 'flow', 'currency', 'is_active']
    search_fields = ['name', 'slug', 'system__slug', 'provider__name']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    ordering = ['-created_at']

    fieldsets = (
        ('Identity', {'fields': ('system', 'name', 'slug', 'description')}),
        ('Processing', {'fields': ('provider', 'provider_account', 'flow', 'auto_capture', 'capture_delay_hours')}),
        ('Amount', {'fields': ('fixed_amount', 'currency')}),
        ('Webhook / Callback', {
            'fields': ('callback_destination',),
            'classes': ('wide',),
            'description': "Controls which system receives payment status callbacks / webhooks."
        }),
        ('Status', {'fields': ('is_active',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        return format_html('<span style="color:#28a745">Yes</span>' if obj.is_active else '<span style="color:#dc3545">No</span>')


@admin.register(PaymentMethodToken)
class PaymentMethodTokenAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'system', 'provider', 'token_type', 'card_brand', 'expiry', 'is_active_colored']
    list_filter = ['system', 'provider', 'token_type', 'is_active']
    search_fields = ['masked_identifier', 'customer_ref', 'system__slug']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    ordering = ['-created_at']

    fieldsets = (
        ('Token', {'fields': ('system', 'provider', 'token_type', 'provider_token')}),
        ('Details', {'fields': ('masked_identifier', 'card_brand', ('expiry_month', 'expiry_year'), 'customer_ref')}),
        ('Status', {'fields': ('is_active',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Expiry')
    def expiry(self, obj):
        if obj.expiry_month and obj.expiry_year:
            return f"{obj.expiry_month:02d}/{obj.expiry_year}"
        return "—"

    @admin.display(description='Active')
    def is_active_colored(self, obj):
        return format_html('<span style="color:#28a745">Yes</span>' if obj.is_active else '<span style="color:#dc3545">No</span>')


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'system',
        'status_colored',
        'amount_currency',
        'chargeable_event',
        'payment_method_token_short',
        'created_at_display',
    ]
    list_filter = ['system', 'status', 'currency', 'chargeable_event', 'created_at']
    search_fields = ['idempotency_key', 'external_reference', 'system__slug']
    readonly_fields = [
        'created_at', 'updated_at', 'synced', 'id',
        'amount_authorized', 'amount_captured', 'amount_refunded', 'amount_settled',
    ]
    inlines = [TransactionInline]
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    fieldsets = (
        ('Core', {'fields': (('system', 'source_system'), 'chargeable_event', 'payment_method_token', ('amount', 'currency'))}),
        ('Amounts', {'fields': (('amount_authorized', 'amount_captured'), ('amount_refunded', 'amount_settled')), 'classes': ('wide',)}),
        ('State', {'fields': ('status', 'next_action', 'expires_at')}),
        ('References', {'fields': ('idempotency_key', 'external_reference', 'payment_payload'), 'classes': ('wide',)}),
        ('Metadata', {'fields': ('metadata',), 'classes': ('collapse',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Status')
    def status_colored(self, obj):
        colors = {
            'CAPTURED': '#28a745', 'SETTLED': '#28a745',
            'AUTHORIZED': '#17a2b8',
            'FAILED': '#dc3545', 'CANCELLED': '#dc3545', 'EXPIRED': '#dc3545',
            'REFUNDED': '#6f42c1',
            'PENDING': '#fd7e14', 'PROCESSING': '#fd7e14',
        }
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(obj.status, '#6c757d'), obj.status
        )

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Token')
    def payment_method_token_short(self, obj):
        if not obj.payment_method_token:
            return "—"
        s = str(obj.payment_method_token)
        return s[:24] + "…" if len(s) > 24 else s

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = [
        '__str__',
        'payment_intent_link',
        'transaction_type',
        'status_colored',
        'amount_currency',
        'provider',
        'created_at_display',
    ]
    list_filter = ['transaction_type', 'status', 'provider', 'currency', 'created_at']
    search_fields = ['provider_transaction_id', 'provider_reference', 'payment_intent__idempotency_key']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id', 'celery_task_id']
    inlines = [
        TransactionStateLogInline,
        WebhookOutboxInline
    ]
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    fieldsets = (
        ('Core', {'fields': ('payment_intent', 'transaction_type', 'status')}),
        ('Provider', {'fields': ('provider', 'provider_account', 'provider_transaction_id', 'provider_reference')}),
        ('Amount', {'fields': (('amount', 'currency'),)}),
        ('Payloads', {'fields': ('request_payload', 'response_payload'), 'classes': ('wide', 'collapse')}),
        ('Failure', {'fields': ('failure_reason', 'failure_code'), 'classes': ('collapse',)}),
        ('Timing', {'fields': ('provider_callback_received_at', 'reconciliation_due_at', 'reconciliation_attempts')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id', 'celery_task_id'), 'classes': ('collapse',)}),
    )

    @admin.display(description='Intent')
    def payment_intent_link(self, obj):
        if not obj.payment_intent:
            return "—"
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
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>',
            colors.get(obj.status, '#6c757d'), obj.status
        )

    @admin.display(description='Amount')
    def amount_currency(self, obj):
        return f"{obj.amount} {obj.currency}"

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(TransactionStateLog)
class TransactionStateLogAdmin(admin.ModelAdmin):
    list_display = ['transaction', 'from_status', 'to_status', 'created_at_display', 'actor']
    list_filter = ['transaction__payment_intent__system', 'created_at']
    search_fields = ['transaction__id', 'reason', 'actor']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    fieldsets = (
        (None, {'fields': ('transaction', 'from_status', 'to_status', 'reason', 'actor', 'metadata')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
    )

    def has_add_permission(self, request):
        return False

    @admin.display(description='When', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


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
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    ordering = ['code']

    fieldsets = (
        (None, {'fields': ('code', 'name', 'account_type', 'currency', 'system', 'is_active')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
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
        'created_at_display',
    ]
    list_filter = ['entry_type', 'currency', 'account__account_type', 'created_at']
    search_fields = ['transaction__id', 'account__code', 'description']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    fieldsets = (
        (None, {'fields': ('transaction', 'account', 'entry_type', 'amount', 'currency', 'description', 'posting_ref')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
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

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(WebhookOutbox)
class WebhookOutboxAdmin(admin.ModelAdmin):
    list_display = [
        'system',
        'event_type',
        'status_colored',
        'attempt_count_display',
        'next_attempt_at',
        'payment_intent_short',
        'created_at_display',
    ]
    list_filter = ['system', 'status', 'event_type', 'next_attempt_at']
    search_fields = ['payment_intent__idempotency_key', 'event_type']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    inlines = [WebhookDeliveryLogInline]
    date_hierarchy = 'next_attempt_at'
    ordering = ['-next_attempt_at']

    fieldsets = (
        ('Target', {'fields': ('system', 'destination_url')}),
        ('Event', {'fields': ('payment_intent', 'transaction', 'event_type', 'payload')}),
        ('Delivery', {'fields': ('status', 'attempt_count', 'max_attempts', 'next_attempt_at', 'last_attempted_at')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
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
        return format_html('<span style="color:{};font-weight:500">{}</span>', colors.get(obj.status, '#6c757d'), obj.status)

    @admin.display(description='Attempts')
    def attempt_count_display(self, obj):
        if obj.attempt_count == 0:
            return "—"
        color = '#dc3545' if obj.status in ['FAILED', 'EXHAUSTED'] else '#6c757d'
        return format_html('<span style="color:{}">{}/{}</span>', color, obj.attempt_count, obj.max_attempts)

    @admin.display(description='Intent')
    def payment_intent_short(self, obj):
        if not obj.payment_intent:
            return "—"
        return f"PI-{obj.payment_intent.id}"

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(WebhookDeliveryLog)
class WebhookDeliveryLogAdmin(admin.ModelAdmin):
    list_display = [
        'outbox_link',
        'attempt_number',
        'response_status_colored',
        'duration_display',
        'created_at_display',
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
        'created_at', 'updated_at', 'synced', 'id',
        'outbox', 'attempt_number',
        'request_headers', 'request_payload',
        'response_status_code', 'response_body', 'response_headers',
        'duration_ms', 'error_message',
    ]
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    list_per_page = 40

    fieldsets = (
        ('Webhook Outbox', {'fields': ('outbox', 'attempt_number')}),
        ('Request', {'fields': ('request_headers', 'request_payload'), 'classes': ('wide', 'collapse')}),
        ('Response', {'fields': ('response_status_code', 'response_body', 'response_headers', 'duration_ms'), 'classes': ('wide',)}),
        ('Error', {'fields': ('error_message',), 'classes': ('collapse',)}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
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
        event = obj.outbox.event_type
        event_short = (event[:28] + "…") if len(event) > 28 else event
        return format_html('<a href="{}">{} — {}</a>', url, f"Outbox #{obj.outbox.id}", event_short)

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
        return f"{obj.duration_ms / 1000:.2f} s"

    @admin.display(description='Error')
    def error_short(self, obj):
        if not obj.error_message:
            return "—"
        msg = obj.error_message
        return (msg[:60] + "…") if len(msg) > 60 else msg

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(ProviderCallbackLog)
class ProviderCallbackLogAdmin(admin.ModelAdmin):
    list_display = [
        'provider',
        'transaction',
        'status_colored',
        'parsed_status',
        'created_at_display',
    ]
    list_filter = ['status', 'provider', 'created_at']
    search_fields = ['raw_payload', 'processing_error', 'provider_reference']
    readonly_fields = ['created_at', 'updated_at', 'id']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    fieldsets = (
        ('Provider & Transaction', {
            'fields': ('provider', 'transaction'),
        }),
        ('Payload', {
            'fields': ('raw_headers', 'raw_payload'),
            'classes': ('wide',),
        }),
        ('Processing', {
            'fields': ('status', 'parsed_status', 'processing_error'),
        }),
        ('Audit', {
            'fields': ('created_at', 'updated_at', 'id'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Status', ordering='status')
    def status_colored(self, obj):
        colors = {
            ProviderCallbackLog.Status.RECEIVED:   '#6c757d',
            ProviderCallbackLog.Status.PROCESSING: '#007bff',
            ProviderCallbackLog.Status.PROCESSED:  '#28a745',
            ProviderCallbackLog.Status.FAILED:     '#dc3545',
            ProviderCallbackLog.Status.REJECTED:   '#dc3545',
            ProviderCallbackLog.Status.IGNORED:    '#fd7e14',
        }
        color = colors.get(obj.status, '#6c757d')
        return format_html(
            '<span style="color:{}; font-weight:bold;">{}</span>',
            color,
            obj.get_status_display()
        )

    @admin.display(description='Created', ordering='created_at')
    def created_at_display(self, obj):
        return format_datetime_admin(obj.created_at)


@admin.register(ReconciliationRecord)
class ReconciliationRecordAdmin(admin.ModelAdmin):
    list_display = [
        'transaction_link',
        'status_colored',
        'provider_reported_status',
        'attempts',
        'last_attempted_at',
    ]
    list_filter = ['status', 'created_at']
    search_fields = ['transaction__id', 'discrepancy_notes']
    readonly_fields = ['created_at', 'updated_at', 'synced', 'id']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']

    def has_add_permission(self, request):
        return False

    fieldsets = (
        ('Transaction', {'fields': ('transaction',)}),
        ('Status', {'fields': ('status', 'provider_reported_status')}),
        ('Resolution', {'fields': ('discrepancy_notes', 'resolved_by', 'resolved_at')}),
        ('Attempts', {'fields': ('attempts', 'last_attempted_at')}),
        ('Audit', {'fields': ('created_at', 'updated_at', 'synced', 'id'), 'classes': ('collapse',)}),
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
        return format_html('<span style="color:{};font-weight:600">{}</span>', colors.get(obj.status, '#6c757d'), obj.status)

    @admin.display(description='Transaction')
    def transaction_link(self, obj):
        url = reverse("admin:core_transaction_change", args=[obj.transaction.id])
        return format_html('<a href="{}">{}</a>', url, f"TXN-{obj.transaction.id}")