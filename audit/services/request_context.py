import threading

from utils.common import get_client_ip


class RequestContext:
    _storage = threading.local()
    _expected_attrs = {
        "request": None,
        "api_client": None,
        "user": None,
        "is_authenticated": None,
        "ip_address": None,
        "user_agent": "",
        "session_key": None,
        "request_id": None,
        "request_data": None,
        "request_headers": None,
        "view_name": None,
        "view_args": None,
        "view_kwargs": None,
        "activity_name": None,
        "response_status": None,
        "response_data": None,
        "response_headers": None,
        "exception_type": None,
        "exception_message": None,
        "request_method": None,
        "request_path": None,
        "is_secure": False,
        "started_at": None,
    }

    def __init__(self):
        if not hasattr(self._storage, "data"):
            for key, default in self._expected_attrs.items():
                setattr(self._storage, key, default)

    @classmethod
    def set(cls, request=None, **kwargs):
        if not hasattr(cls._storage, "request_id"):
            for key, default in cls._expected_attrs.items():
                setattr(cls._storage, key, default)

        for key, value in kwargs.items():
            if key in cls._expected_attrs:
                setattr(cls._storage, key, value)

        if request:
            cls._storage.request = request
            if not cls._storage.user and hasattr(request, "user"):
                cls._storage.user = request.user if request.user.is_authenticated else None
            if not cls._storage.ip_address:
                cls._storage.ip_address = get_client_ip(request)
            if not cls._storage.user_agent:
                cls._storage.user_agent = request.META.get("HTTP_USER_AGENT", "")
            if not cls._storage.session_key:
                cls._storage.session_key = getattr(request.session, "session_key", None)

    @classmethod
    def get(cls) -> dict[str, object | None]:
        context = {k: getattr(cls._storage, k, v) for k, v in cls._expected_attrs.items()}
        if not context.get("activity_name") and context.get("view_name"):
            context["activity_name"] = cls._humanize_view_name(context["view_name"])
        return context

    @classmethod
    def update(cls, **kwargs):
        for key, value in kwargs.items():
            if key in cls._expected_attrs:
                setattr(cls._storage, key, value)

    @classmethod
    def clear(cls):
        for key in cls._expected_attrs:
            if hasattr(cls._storage, key):
                delattr(cls._storage, key)

    @classmethod
    def exists(cls) -> bool:
        return hasattr(cls._storage, "request_id") and cls._storage.request_id is not None

    @staticmethod
    def _humanize_view_name(name: str) -> str:
        if "_" in name:
            return " ".join(word.capitalize() for word in name.split("_"))
        else:
            result = []
            for char in name:
                if char.isupper() and result:
                    result.append(" ")
                result.append(char)
            return "".join(result).capitalize()
