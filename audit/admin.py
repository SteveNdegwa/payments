from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from .models import AuditConfiguration, AuditLog, RequestLog


class RequestPathCategoryFilter(SimpleListFilter):
    title = _("Request Path Category")
    parameter_name = "path_category"

    def lookups(self, request, model_admin):
        return [
            ("api", _("API Calls (/api…)")),
            ("admin", _("Admin (/cia…)")),
            ("other", _("Other")),
        ]

    def queryset(self, request, queryset):
        if self.value() == "api":
            return queryset.filter(request_path__startswith="/api")

        if self.value() == "admin":
            return queryset.filter(models.Q(request_path__startswith="/cia"))

        if self.value() == "other":
            return queryset.exclude(
                models.Q(request_path__startswith="/api")
                | models.Q(request_path__startswith="/cia")
            )

        return queryset


@admin.register(RequestLog)
class RequestLogAdmin(admin.ModelAdmin):
    list_display = (
        "request_id_short",
        "api_client",
        "user",
        "activity_name",
        "request_method",
        "colored_status",
        "request_path_short",
        "started_at_relative",
        "time_taken_ms",
        "related_audits_link",
    )
    list_filter = (
        RequestPathCategoryFilter,
        "api_client",
        "is_authenticated",
        "request_method",
        "response_status",
        "activity_name",
        "started_at",
    )
    search_fields = (
        "request_id",
        "api_client__name",
        "api_client__slug",
        "user__username",
        "user__email",
        "ip_address",
        "request_path",
        "activity_name",
        "view_name",
        "exception_type",
        "exception_message",
    )
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    list_per_page = 40

    readonly_fields = (
        "request_id",
        "started_at",
        "ended_at",
        "time_taken",
        "response_status",
        "response_headers",
        "response_data",
        "id",
        "created_at",
        "updated_at",
        "synced",
    )

    fieldsets = (
        (
            _("Request Identification"),
            {
                "fields": (
                    "request_id",
                    "request_method",
                    "request_path",
                    "is_secure",
                )
            },
        ),
        (
            _("Client & Authentication"),
            {
                "fields": (
                    "api_client",
                    "user,is_authenticated",
                    "ip_address",
                    "user_agent",
                    "session_key",
                )
            },
        ),
        (
            _("View & Context"),
            {
                "fields": (
                    "view_name",
                    "view_args",
                    "view_kwargs",
                    "activity_name",
                ),
                "classes": ("wide",),
            },
        ),
        (
            _("Timing"),
            {
                "fields": (
                    "started_at",
                    "ended_at",
                    "time_taken",
                )
            },
        ),
        (
            _("Response"),
            {
                "fields": (
                    "response_status",
                    "response_headers",
                    "response_data",
                ),
                "classes": ("wide", "collapse"),
            },
        ),
        (
            _("Exception"),
            {
                "fields": (
                    "exception_type",
                    "exception_message",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Audit Fields"),
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "synced",
                    "id",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Request ID"), ordering="request_id")
    def request_id_short(self, obj):
        return str(obj.request_id)[:8] + "…"

    @admin.display(description=_("Path"))
    def request_path_short(self, obj):
        if len(obj.request_path) > 60:
            return obj.request_path[:60] + "…"
        return obj.request_path

    @admin.display(description=_("Status"), ordering="response_status")
    def colored_status(self, obj):
        if obj.response_status is None:
            return format_html('<span style="color:#6c757d;">—</span>')

        code = obj.response_status
        if 200 <= code < 300:
            color = "#28a745"  # success green
        elif 300 <= code < 400:
            color = "#f59e0b"  # redirect amber
        elif 400 <= code < 500:
            color = "#f97316"  # client error orange
        else:
            color = "#ef4444"  # server error red

        return format_html('<b style="color:{};">{}</b>', color, code)

    @admin.display(description=_("Duration"), ordering="time_taken")
    def time_taken_ms(self, obj):
        if obj.time_taken is None:
            return "—"

        ms = int(obj.time_taken * 1000)
        if ms < 1000:
            return f"{ms} ms"
        return f"{ms / 1000:.2f} s"

    @admin.display(description=_("Started"))
    def started_at_relative(self, obj):
        delta = timezone.now() - obj.started_at
        if delta.total_seconds() < 90:
            return format_html('<span style="color:#10b981;">just now</span>')
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() // 60)} min ago"
        if delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        return f"{delta.days}d ago"

    @admin.display(description=_("Related Audits"))
    def related_audits_link(self, obj):
        count = AuditLog.objects.filter(request_id=obj.request_id).count()
        if count == 0:
            return "—"

        url = reverse("admin:audit_auditlog_changelist") + f"?request_id={obj.request_id}"
        color = "#3b82f6" if count <= 3 else "#ef4444"

        return format_html(
            '<a href="{}" style="color:{};">{} audit log{}<a>',
            url,
            color,
            count,
            "s" if count != 1 else "",
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = (
        "summary",
        "created_at_relative",
        "colored_severity",
        "event_type",
        "user_or_system",
        "content_object_repr",
        "view_object_link",
        "view_request_link",
    )
    list_filter = (
        "event_type",
        "severity",
        "created_at",
        "content_type",
        "api_client",
    )
    search_fields = (
        "object_repr",
        "object_id",
        "user__username",
        "user__email",
        "request_id",
        "activity_name",
        "changes",
        "metadata",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 40

    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "synced",
        "request_id",
        "user",
        "api_client",
        "ip_address",
        "user_agent",
        "request_method",
        "request_path",
        "activity_name",
        "event_type",
        "severity",
        "content_type",
        "object_id",
        "object_repr",
        "changes",
        "metadata",
    )

    fieldsets = (
        (
            _("Actor & Context"),
            {
                "fields": (
                    "user",
                    "api_client",
                    "ip_address",
                    "user_agent",
                    "request_method",
                    "request_path",
                    "activity_name",
                    "request_id",
                    "view_request_link",
                )
            },
        ),
        (
            _("Event"),
            {
                "fields": (
                    "event_type",
                    "severity",
                )
            },
        ),
        (
            _("Affected Object"),
            {
                "fields": (
                    "content_type",
                    "object_id",
                    "object_repr",
                    "view_object_link",
                )
            },
        ),
        (
            _("Changes"),
            {
                "fields": ("changes",),
                "classes": ("wide", "collapse"),
            },
        ),
        (
            _("Metadata"),
            {
                "fields": ("metadata",),
                "classes": ("wide", "collapse"),
            },
        ),
        (
            _("System Fields"),
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "synced",
                    "id",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("When"), ordering="created_at")
    def created_at_relative(self, obj):
        delta = timezone.now() - obj.created_at
        if delta.total_seconds() < 90:
            return format_html('<span style="color:#10b981;">just now</span>')
        if delta.total_seconds() < 3600:
            return f"{int(delta.total_seconds() // 60)} min ago"
        if delta.days == 0:
            return f"{int(delta.total_seconds() // 3600)} h ago"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return obj.created_at.strftime("%Y-%m-%d")

    @admin.display(description=_("Summary"))
    def summary(self, obj):
        model_label = obj.content_type.model_class().__name__ if obj.content_type else "Unknown"

        action_map = {
            "create": "Created",
            "update": "Updated",
            "delete": "Deleted",
            "view": "Viewed",
        }
        action = action_map.get(obj.event_type, obj.event_type.title())

        if obj.object_repr:
            repr_part = f" ({obj.object_repr})"
        elif obj.object_id:
            repr_part = f" (ID: {obj.object_id})"
        else:
            repr_part = ""

        return f"{action} {model_label}{repr_part}"

    @admin.display(description=_("Severity"), ordering="severity")
    def colored_severity(self, obj):
        colors = {
            "low": "#10b981",  # emerald-500
            "medium": "#f59e0b",  # amber-500
            "high": "#ef4444",  # red-500
            "critical": "#b91c1c",  # red-700
        }
        color = colors.get(obj.severity.lower(), "#6b7280")
        return format_html(
            '<span style="color:{}; font-weight:500;">{}</span>', color, obj.severity.title()
        )

    @admin.display(description=_("Actor"))
    def user_or_system(self, obj):
        if obj.user:
            return obj.user.get_username()
        return format_html('<span style="color:#6b7280;">System</span>')

    @admin.display(description=_("Object"))
    def content_object_repr(self, obj):
        if obj.object_repr:
            if len(obj.object_repr) > 50:
                return obj.object_repr[:50] + "…"
            return obj.object_repr
        if obj.object_id:
            return f"ID: {obj.object_id}"
        return "—"

    @admin.display(description=_("Object"))
    def view_object_link(self, obj):
        if not obj.content_type or not obj.object_id:
            return "—"

        try:
            model_class = obj.content_type.model_class()
            if not model_class:
                return "—"

            url = reverse(
                f"admin:{obj.content_type.app_label}_{obj.content_type.model}_change",
                args=[obj.object_id],
            )
            return format_html('<a href="{}">{}</a>', url, _("View"))
        except Exception:
            return format_html('<span style="color:#9ca3af;">Deleted / N/A</span>')

    @admin.display(description=_("Request"))
    def view_request_link(self, obj):
        if not obj.request_id:
            return "—"

        try:
            req = RequestLog.objects.filter(request_id=obj.request_id).first()
            if not req:
                return "—"

            url = reverse("admin:audit_requestlog_change", args=[req.pk])
            return format_html('<a href="{}">{}</a>', url, _("View Request"))
        except Exception:
            return "—"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditConfiguration)
class AuditConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "app_label",
        "model_name",
        "is_enabled_colored",
        "track_create",
        "track_update",
        "track_delete",
        "retention_days",
    )
    list_filter = (
        "is_enabled",
        "track_create",
        "track_update",
        "track_delete",
        "app_label",
    )
    search_fields = (
        "app_label",
        "model_name",
    )
    readonly_fields = (
        "id",
        "created_at",
        "updated_at",
        "synced",
    )
    list_per_page = 50
    ordering = ("app_label", "model_name")

    fieldsets = (
        (
            _("Model"),
            {
                "fields": (
                    "app_label",
                    "model_name",
                )
            },
        ),
        (
            _("Tracking"),
            {
                "fields": (
                    "is_enabled",
                    "track_create",
                    "track_update",
                    "track_delete",
                    "excluded_fields",
                )
            },
        ),
        (_("Retention"), {"fields": ("retention_days",)}),
        (
            _("System"),
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "synced",
                    "id",
                ),
                "classes": ("collapse",),
            },
        ),
    )

    @admin.display(description=_("Enabled"), ordering="is_enabled")
    def is_enabled_colored(self, obj):
        if obj.is_enabled:
            return format_html('<span style="color:#10b981;">Yes</span>')
        return format_html('<span style="color:#ef4444;">No</span>')
