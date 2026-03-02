from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from datetime import timedelta

from .models import RateLimitRule, RateLimitAttempt, RateLimitBlock


class HasBlockDurationFilter(admin.SimpleListFilter):
    title = 'Has Block Duration'
    parameter_name = 'has_block'

    def lookups(self, request, model_admin):
        return (
            ('yes', 'Yes (>0 min)'),
            ('no',  'No (0 min)'),
        )

    def queryset(self, request, queryset):
        if self.value() == 'yes':
            return queryset.filter(block_duration_minutes__gt=0)
        if self.value() == 'no':
            return queryset.filter(block_duration_minutes=0)
        return queryset


class RecentRateLimitAttemptInline(admin.TabularInline):
    model = RateLimitAttempt
    extra = 0
    max_num = 12
    can_delete = False
    fields = [
        'key_short',
        'endpoint_short',
        'method',
        'count_colored',
        'window_start',
        'last_attempt_relative',
    ]
    readonly_fields = [
        'key_short',
        'endpoint_short',
        'method',
        'count_colored',
        'window_start',
        'last_attempt_relative',
    ]
    ordering = ['-last_attempt']
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Key')
    def key_short(self, obj):
        return obj.key[:22] + '…' if len(obj.key) > 22 else obj.key

    @admin.display(description='Endpoint')
    def endpoint_short(self, obj):
        return obj.endpoint[:32] + '…' if len(obj.endpoint) > 32 else obj.endpoint

    @admin.display(description='Count')
    def count_colored(self, obj):
        if obj.count >= obj.rule.limit:
            return format_html(
                '<span style="color:#dc3545;font-weight:bold">{}</span>',
                obj.count
            )
        elif obj.count >= obj.rule.limit * 0.75:
            return format_html(
                '<span style="color:#fd7e14">{}</span>',
                obj.count
            )
        return obj.count

    @admin.display(description='Window')
    def window_start(self, obj):
        return obj.window_start.strftime('%Y-%m-%d %H:%M')

    @admin.display(description='Last attempt')
    def last_attempt_relative(self, obj):
        delta = timezone.now() - obj.last_attempt
        if delta.total_seconds() < 120:
            return format_html('<span style="color:#28a745">just now</span>')
        elif delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds()//60)} min ago"
        elif delta.days < 1:
            return f"{int(delta.total_seconds()//3600)} h ago"
        else:
            return f"{delta.days}d ago"


class ActiveRateLimitBlockInline(admin.TabularInline):
    model = RateLimitBlock
    extra = 0
    max_num = 8
    can_delete = False
    fields = [
        'key_short',
        'blocked_until',
        'status_colored',
        'duration_left',
        'created_relative',
    ]
    readonly_fields = [
        'key_short',
        'blocked_until',
        'status_colored',
        'duration_left',
        'created_relative',
    ]
    ordering = ['-blocked_until']
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description='Key')
    def key_short(self, obj):
        return obj.key[:22] + '…' if len(obj.key) > 22 else obj.key

    @admin.display(description='Blocked until')
    def blocked_until(self, obj):
        return obj.blocked_until.strftime('%Y-%m-%d %H:%M')

    @admin.display(description='Status')
    def status_colored(self, obj):
        now = timezone.now()
        if obj.blocked_until > now:
            return format_html(
                '<span style="color:#dc3545;font-weight:600">BLOCKED</span>'
            )
        return format_html(
            '<span style="color:#6c757d">expired</span>'
        )

    @admin.display(description='Time left')
    def duration_left(self, obj):
        now = timezone.now()
        if obj.blocked_until <= now:
            return "—"
        delta = obj.blocked_until - now
        total_min = int(delta.total_seconds() // 60)
        if total_min < 60:
            return f"{total_min} min"
        h = total_min // 60
        m = total_min % 60
        return f"{h}h {m:02d}min" if m else f"{h}h"

    @admin.display(description='Created')
    def created_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds()//60)} min ago"
        elif delta.days == 0:
            return f"{int(delta.total_seconds()//3600)} h ago"
        else:
            return f"{delta.days}d ago"


@admin.register(RateLimitRule)
class RateLimitRuleAdmin(admin.ModelAdmin):
    list_display = [
        'name_colored',
        'scope',
        'limit_display',
        'period_display',
        'is_active_colored',
        'priority',
        'block_duration_display',
        'endpoint_pattern_short',
        'http_methods_short',
        'attempt_count_last_24h',
    ]
    list_filter = [
        'scope',
        'is_active',
        'period',
        'priority',
        HasBlockDurationFilter,
    ]
    search_fields = [
        'name',
        'endpoint_pattern',
        'http_methods',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
    ]
    inlines = [
        RecentRateLimitAttemptInline,
        ActiveRateLimitBlockInline,
    ]

    fieldsets = (
        ('Main Settings', {
            'fields': (
                'name',
                'is_active',
                'priority',
            ),
        }),
        ('Limit Definition', {
            'fields': (
                'scope',
                'limit',
                ('period', 'period_count'),
            ),
            'classes': ('wide',),
        }),
        ('Matching Criteria', {
            'fields': (
                'endpoint_pattern',
                'http_methods',
            ),
            'classes': ('wide',),
        }),
        ('Consequence', {
            'fields': (
                'block_duration_minutes',
            ),
            'description': '0 = only count, no blocking',
        }),
        ('Audit & System', {
            'fields': (
                ('created_at', 'updated_at'),
                'synced',
                'id',
            ),
            'classes': ('collapse',),
        }),
    )

    list_per_page = 25
    ordering = ['-priority', 'name']

    @admin.display(description='Rule', ordering='name')
    def name_colored(self, obj):
        color = '#0066cc' if obj.is_active else '#888888'
        return format_html('<span style="color: {};">{}</span>', color, obj.name)

    @admin.display(description='Limit')
    def limit_display(self, obj):
        return obj.limit

    @admin.display(description='Period')
    def period_display(self, obj):
        txt = f"{obj.period_count} {obj.period}"
        if obj.period_count == 1:
            txt = txt.rstrip('s')
        return txt

    @admin.display(description='Active', ordering='is_active')
    def is_active_colored(self, obj):
        if obj.is_active:
            return format_html('<span style="color: #28a745; font-weight: 500;">Yes</span>')
        return format_html('<span style="color: #dc3545; font-weight: 500;">No</span>')

    @admin.display(description='Block')
    def block_duration_display(self, obj):
        if obj.block_duration_minutes == 0:
            return "—"
        return f"{obj.block_duration_minutes} min"

    @admin.display(description='Pattern')
    def endpoint_pattern_short(self, obj):
        if not obj.endpoint_pattern:
            return "—"
        return obj.endpoint_pattern[:38] + "…" if len(obj.endpoint_pattern) > 38 else obj.endpoint_pattern

    @admin.display(description='Methods')
    def http_methods_short(self, obj):
        if not obj.http_methods:
            return "ALL"
        return obj.http_methods[:30] + "…" if len(obj.http_methods) > 30 else obj.http_methods

    @admin.display(description='Attempts 24h')
    def attempt_count_last_24h(self, obj):
        since = timezone.now() - timedelta(hours=24)
        count = RateLimitAttempt.objects.filter(
            rule=obj,
            last_attempt__gte=since
        ).count()
        if count == 0:
            return "—"
        elif count < 10:
            return str(count)
        elif count < 50:
            return format_html('<span style="color:#fd7e14">{}</span>', count)
        else:
            return format_html('<span style="color:#dc3545;font-weight:bold">{}</span>', count)


@admin.register(RateLimitAttempt)
class RateLimitAttemptAdmin(admin.ModelAdmin):
    list_display = [
        'rule_link',
        'key_short',
        'endpoint_short',
        'method',
        'count_colored',
        'window_start',
        'last_attempt_relative',
    ]
    list_filter = [
        'rule',
        'method',
        'window_start',
    ]
    search_fields = [
        'key',
        'endpoint',
        'rule__name',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'rule',
        'key',
        'endpoint',
        'method',
        'window_start',
    ]
    date_hierarchy = 'window_start'
    ordering = ['-last_attempt']
    list_per_page = 40

    fieldsets = (
        ('Request Info', {
            'fields': (
                'rule',
                'key',
                'endpoint',
                'method',
            ),
        }),
        ('Count & Window', {
            'fields': (
                'count',
                'window_start',
                'last_attempt',
            ),
        }),
        ('System', {
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

    @admin.display(description='Rule')
    def rule_link(self, obj):
        url = reverse("admin:rate_limit_ratelimitrule_change", args=[obj.rule.id])
        return format_html('<a href="{}">{}</a>', url, obj.rule.name)

    @admin.display(description='Key')
    def key_short(self, obj):
        return obj.key[:24] + "…" if len(obj.key) > 24 else obj.key

    @admin.display(description='Endpoint')
    def endpoint_short(self, obj):
        return obj.endpoint[:38] + "…" if len(obj.endpoint) > 38 else obj.endpoint

    @admin.display(description='Count', ordering='count')
    def count_colored(self, obj):
        if obj.count >= obj.rule.limit:
            return format_html(
                '<span style="color: #dc3545; font-weight: bold;">{}</span>',
                obj.count
            )
        elif obj.count >= obj.rule.limit * 0.8:
            return format_html(
                '<span style="color: #fd7e14;">{}</span>',
                obj.count
            )
        return obj.count

    @admin.display(description='Last')
    def last_attempt_relative(self, obj):
        delta = timezone.now() - obj.last_attempt
        if delta.total_seconds() < 90:
            return "just now"
        elif delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() // 60)} min ago"
        elif delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        else:
            return f"{delta.days}d ago"


@admin.register(RateLimitBlock)
class RateLimitBlockAdmin(admin.ModelAdmin):
    list_display = [
        'rule_link',
        'key_short',
        'blocked_until',
        'status_colored',
        'duration_left',
        'created_relative',
    ]
    list_filter = [
        'rule',
        'blocked_until',
    ]
    search_fields = [
        'key',
        'rule__name',
    ]
    readonly_fields = [
        'created_at',
        'updated_at',
        'synced',
        'id',
        'rule',
        'key',
        'blocked_until',
    ]
    date_hierarchy = 'blocked_until'
    ordering = ['-blocked_until']

    fieldsets = (
        ('Block Info', {
            'fields': (
                'rule',
                'key',
                'blocked_until',
            ),
        }),
        ('System', {
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

    @admin.display(description='Rule')
    def rule_link(self, obj):
        url = reverse("admin:rate_limit_ratelimitrule_change", args=[obj.rule.id])
        return format_html('<a href="{}">{}</a>', url, obj.rule.name)

    @admin.display(description='Key')
    def key_short(self, obj):
        return obj.key[:24] + "…" if len(obj.key) > 24 else obj.key

    @admin.display(description='Status', ordering='blocked_until')
    def status_colored(self, obj):
        now = timezone.now()
        if obj.blocked_until > now:
            return format_html(
                '<span style="color: #dc3545; font-weight: 600;">Blocked</span>'
            )
        return format_html(
            '<span style="color: #6c757d;">Expired</span>'
        )

    @admin.display(description='Remaining')
    def duration_left(self, obj):
        now = timezone.now()
        if obj.blocked_until <= now:
            return "—"
        delta = obj.blocked_until - now
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours}h {mins}min" if mins else f"{hours}h"

    @admin.display(description='Created')
    def created_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() // 60)} min ago"
        elif delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        else:
            return f"{delta.days}d ago"