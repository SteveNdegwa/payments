import json
import logging
from typing import Optional, Any

from django.core.handlers.wsgi import WSGIRequest

logger = logging.getLogger(__name__)


def get_request_data(request: WSGIRequest) -> tuple[dict, dict]:
    try:
        if request is None:
            return {}, {}

        method = request.method
        content_type = request.META.get('CONTENT_TYPE', '')

        data = {}
        files = {}

        if 'application/json' in content_type:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                data = {}
        elif method in ['POST', 'PUT', 'PATCH']:
            data = request.POST.dict()
        elif method == 'GET':
            data = request.GET.dict()

        if request.FILES:
            files = {
                key: request.FILES.getlist(key) if len(request.FILES.getlist(key)) > 1
                else request.FILES[key]
                for key in request.FILES
            }

        if not data and request.body:
            # noinspection PyBroadException
            try:
                data = json.loads(request.body)
            except Exception:
                data = {}

        return data, files

    except Exception as ex:
        logger.exception('get_request_data Exception: %s' % ex)
        return {}, {}


def sanitize_data(data: Optional[dict]) -> Optional[dict]:
    sensitive_keys = {}

    if data is None:
        return None

    def _sanitize(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: ("****" if k.lower() in sensitive_keys else _sanitize(v))
                for k, v in obj.items()
            }
        elif isinstance(obj, list):
            return [_sanitize(item) for item in obj]
        else:
            return obj

    return _sanitize(data)


def get_client_ip(request: WSGIRequest) -> str:
    def clean_ip(value):
        if not value:
            return None
        value = value.strip()
        return value if value else None

    source_ip = clean_ip(request.META.get('HTTP_X_SOURCE_IP'))
    if source_ip:
        return source_ip

    x_forwarded_for = clean_ip(request.META.get('HTTP_X_FORWARDED_FOR'))
    if x_forwarded_for:
        first_ip = clean_ip(x_forwarded_for.split(',')[0])
        if first_ip:
            return first_ip

    remote_addr = clean_ip(request.META.get('REMOTE_ADDR'))
    return remote_addr
