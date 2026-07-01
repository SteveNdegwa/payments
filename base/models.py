import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _

from audit.mixins import AuditableMixin


class BaseModel(AuditableMixin, models.Model):
    id = models.UUIDField(
        max_length=100, default=uuid.uuid4, unique=True, editable=False, primary_key=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    synced = models.BooleanField(
        default=False,
        help_text=_(
            "Indicates whether this record has been synchronized with the replica database."
        ),
    )

    objects = models.Manager()

    class Meta:
        abstract = True
