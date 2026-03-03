from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils.translation import gettext_lazy as _

from base.models import BaseModel


class RequestLog(BaseModel):
    request_id = models.UUIDField(editable=False)
    api_client = models.ForeignKey('core.System', null=True, on_delete=models.SET_NULL)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    is_authenticated = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    session_key = models.CharField(max_length=40, null=True, blank=True)
    request_method = models.CharField(max_length=10)
    request_path = models.TextField()
    request_headers = models.JSONField(null=True, blank=True)
    request_data = models.JSONField(null=True, blank=True)
    is_secure = models.BooleanField(default=False)
    view_name = models.CharField(max_length=255, null=True, blank=True)
    view_args = models.JSONField(null=True, blank=True)
    view_kwargs = models.JSONField(null=True, blank=True)
    activity_name = models.CharField(max_length=255, null=True, blank=True)
    exception_type = models.CharField(max_length=255, null=True, blank=True)
    exception_message = models.TextField(null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    time_taken = models.FloatField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_headers = models.JSONField(null=True, blank=True)
    response_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name = _("Request Log")
        verbose_name_plural = _("Request Logs")
        indexes = [
            models.Index(fields=['started_at']),
            models.Index(fields=['ended_at']),
            models.Index(fields=['time_taken']),
            models.Index(fields=['request_method']),
            models.Index(fields=['is_authenticated']),
            models.Index(fields=['activity_name']),
            models.Index(fields=['view_name']),
        ]

    def __str__(self):
        return f'RequestLog {self.request_id} - {self.request_method} {self.request_path}'


class AuditEventType(models.TextChoices):
    CREATE = 'create', _('Create')
    UPDATE = 'update', _('Update')
    DELETE = 'delete', _('Delete')
    VIEW = 'view', _('View')
    LOGIN = 'login', _('Login')
    LOGOUT = 'logout', _('Logout')
    PERMISSION_CHANGE = 'permission_change', _('Permission Change')
    DATA_EXPORT = 'data_export', _('Data Export')
    BULK_OPERATION = 'bulk_operation', _('Bulk Operation')
    SYSTEM_EVENT = 'system_event', _('System Event')
    SECURITY_EVENT = 'security_event', _('Security Event')


class AuditSeverity(models.TextChoices):
    LOW = 'low', _('Low')
    MEDIUM = 'medium', _('Medium')
    HIGH = 'high', _('High')
    CRITICAL = 'critical', _('Critical')


class AuditLog(BaseModel):
    request_id = models.UUIDField(null=True, blank=True)
    api_client = models.ForeignKey('core.System', null=True, blank=True, on_delete=models.SET_NULL)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    request_method = models.CharField(max_length=10, null=True, blank=True)
    request_path = models.TextField(null=True, blank=True)
    activity_name = models.CharField(max_length=255, null=True, blank=True)
    event_type = models.CharField(
        max_length=50,
        choices=AuditEventType.choices,
        db_index=True
    )
    severity = models.CharField(
        max_length=20,
        choices=AuditSeverity.choices,
        default=AuditSeverity.LOW,
        db_index=True
    )
    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )
    object_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    object_repr = models.CharField(max_length=255, null=True, blank=True)
    changes = models.JSONField(
        null=True,
        blank=True,
        encoder=DjangoJSONEncoder
    )
    metadata = models.JSONField(
        null=True,
        blank=True,
        encoder=DjangoJSONEncoder
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = _("Audit Log")
        verbose_name_plural = _("Audit Logs")
        indexes = [
            models.Index(fields=['created_at', 'event_type']),
            models.Index(fields=['content_type', 'object_id']),
            models.Index(fields=['severity', 'created_at']),
            models.Index(fields=['user']),
            models.Index(fields=['request_id']),
        ]

    def __str__(self):
        actor = self.user or "System"
        return f'{self.created_at} - {self.event_type} by {actor}'


class AuditConfiguration(BaseModel):
    app_label = models.CharField(max_length=100)
    model_name = models.CharField(max_length=100, unique=True)
    is_enabled = models.BooleanField(default=True)
    track_create = models.BooleanField(default=True)
    track_update = models.BooleanField(default=True)
    track_delete = models.BooleanField(default=True)
    excluded_fields = models.JSONField(default=list, blank=True)
    retention_days = models.PositiveIntegerField(default=2555)

    class Meta:
        unique_together = ('app_label', 'model_name')
        ordering = ['app_label', 'model_name']
        verbose_name = _("Audit Configuration")
        verbose_name_plural = _("Audit Configurations")

    def __str__(self):
        return f'{self.app_label}.{self.model_name}'

    def save(self, *args, **kwargs):
        self.app_label = self.app_label.lower()
        self.model_name = self.model_name.lower()
        super().save(*args, **kwargs)
