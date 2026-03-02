from datetime import datetime
from typing import Union, Optional

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest

from core.models import System


class ExtendedRequest(HttpRequest):
    """
    Extends the base HttpRequest with additional attributes.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_client: Optional[System] = None
        self.user: Union[AnonymousUser, User] = AnonymousUser()
        self.client_ip: Optional[str] = None
        self.user_agent: Optional[str] = None
        self.data: dict = {}
        self.received_at: Optional[datetime] = None
