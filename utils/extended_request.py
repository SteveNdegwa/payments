from datetime import datetime

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest

from core.models import System


class ExtendedRequest(HttpRequest):
    """
    Extends the base HttpRequest with additional attributes.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_client: System | None = None
        self.user: AnonymousUser | User = AnonymousUser()
        self.client_ip: str | None = None
        self.user_agent: str | None = None
        self.data: dict = {}
        self.received_at: datetime | None = None
