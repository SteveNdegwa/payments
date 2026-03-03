import hashlib
import json
import logging
import re
import traceback
import uuid
from datetime import timedelta, datetime

from django.core.exceptions import ValidationError, PermissionDenied, ObjectDoesNotExist
from django.db.models import F
from django.db.models.aggregates import Sum
from django.urls import resolve
from django.utils import timezone
from django.contrib.auth.models import AnonymousUser, User

from api.models import RateLimitRule, RateLimitBlock, RateLimitAttempt
from audit.models import RequestLog
from audit.services.request_context import RequestContext
from core.models import System
from utils.common import get_request_data, sanitize_data, get_client_ip
from utils.response_provider import ResponseProvider

logger = logging.getLogger(__name__)


class GatewayControlMiddleware:
    API_KEY_HEADER = "X-Api-Key"

    API_CLIENT_VALIDATION_EXEMPT_PATHS = [
        "/cia",
        "/health",
        "/static",
        "/media",
        "/__debug__",
        "/favicon.ico",
        "/api/v1/core/payments/callbacks"
    ]

    SAVE_REQUEST_LOG_EXEMPT_PATHS = ["/health", "/favicon.ico"]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        api_key_validation_required = True

        if request.path.startswith("/api/"):
            request._dont_enforce_csrf_checks = True

        self._set_request_metadata(request)

        RequestContext.set(
            request=request,
            user=request.user if not isinstance(request.user, AnonymousUser) else None,
            is_authenticated=request.is_authenticated,
            ip_address=request.client_ip,
            user_agent=request.user_agent,
            request_id=str(uuid.uuid4()),
            request_headers=dict(request.headers),
            request_data=sanitize_data(request.data),
            session_key=getattr(request.session, 'session_key', None),
            request_method=request.method,
            request_path=request.path,
            is_secure=request.is_secure(),
            started_at=timezone.now(),
        )

        if any(request.path.startswith(p) for p in self.API_CLIENT_VALIDATION_EXEMPT_PATHS):
            api_key_validation_required = False

        if api_key_validation_required:
            response = self._validate_api_client(request)
            if response:
                return self._process_response(request, response)

            RequestContext.update(api_client=request.api_client)

        rate_limit_result = self._check_rate_limit(request)
        if rate_limit_result.get("blocked"):
            response = ResponseProvider.too_many_requests(error="Rate limit exceeded. Try again later.")
            response = self._set_headers(response, rate_limit_result)
            return self._process_response(request, response)

        # noinspection PyBroadException
        try:
            resolver_match = resolve(request.path)
            view_func = resolver_match.func
            self._process_view(request, view_func, resolver_match.args, resolver_match.kwargs)
        except:
            pass

        try:
            response = self.get_response(request)
        except Exception as exc:
            response = self.process_exception(request, exc)

        response = self._set_headers(response, rate_limit_result)
        return self._process_response(request, response)

    @staticmethod
    def _process_view(request, view_func, view_args, view_kwargs):
        view_name = getattr(view_func, '__name__', 'unknown')
        RequestContext.update(
            view_name=view_name,
            view_args=view_args,
            view_kwargs=view_kwargs,
        )

    @staticmethod
    def process_exception(request, exception):
        handled_types = (ValidationError, ObjectDoesNotExist, PermissionDenied)
        if not isinstance(exception, handled_types):
            logger.error(
                "Unhandled exception\n"
                f"Path: {request.path}\n"
                f"Method: {request.method}\n"
                f"User: {getattr(request.user, 'username', 'Anonymous')}\n"
                f"Exception Type: {type(exception).__name__}\n"
                f"Message: {str(exception)}\n"
                f"Traceback:\n{traceback.format_exc()}"
            )

        RequestContext.update(
            exception_type=type(exception).__name__,
            exception_message=str(exception),
        )
        response = ResponseProvider.handle_exception(exception)

        return response

    def _process_response(self, request, response):
        if hasattr(response, 'status_code'):
            RequestContext.update(response_status=response.status_code)

        if hasattr(response, 'headers'):
            response_headers = dict(response.headers)
            RequestContext.update(response_headers=response_headers)

        # noinspection PyBroadException
        try:
            if hasattr(response, 'data'):
                response_data = response.data
            elif hasattr(response, 'content') and response.get('Content-Type', '').startswith('application/json'):
                response_data = json.loads(response.content)
            else:
                response_data = getattr(response, 'content', '')
                if isinstance(response_data, bytes):
                    response_data = response_data.decode(errors='ignore')
                response_data = response_data[:2000]
        except:
            response_data = f'<Could not parse response: {type(response).__name__}>'

        RequestContext.update(response_data=response_data)
        self._save_request_log()
        RequestContext.clear()

        return response

    @staticmethod
    def _set_request_metadata(request):
        request.api_client = None
        request.user = getattr(request, 'user', None)
        request.is_authenticated = True if isinstance(request.user, User) else False
        request.client_ip = get_client_ip(request)
        request.user_agent = request.headers.get("User-Agent", "")
        request.data, request.files = get_request_data(request)
        request.received_at = timezone.now()

    def _validate_api_client(self, request):
        request.api_client = None

        raw_api_key = request.headers.get(self.API_KEY_HEADER)
        if not raw_api_key:
            return ResponseProvider.unauthorized(error="Missing API key")

        hashed_api_key = hashlib.sha256(raw_api_key.encode("utf-8")).hexdigest()

        system = System.objects.filter(hashed_api_key=hashed_api_key, is_active=True).first()
        if not system:
            logger.warning("Invalid API key attempted from IP %s", request.client_ip)
            return ResponseProvider.unauthorized(error="Invalid API key")

        if system.allowed_ips:
            if request.client_ip not in system.allowed_ips:
                logger.warning("IP %s not allowed for client %s", request.client_ip, system.name)
                return ResponseProvider.forbidden(error="IP address not allowed")

        request.api_client = system
        return None

    @staticmethod
    def _get_window_start(now, window):
        seconds = int(window.total_seconds())
        timestamp = int(now.timestamp())
        bucket = timestamp - (timestamp % seconds)
        return datetime.fromtimestamp(bucket, tz=timezone.get_current_timezone())

    def _check_rate_limit(self, request) -> dict:
        client_ip = getattr(request, "client_ip", None)
        api_client_id = str(request.api_client.id) if getattr(request, "api_client", None) else f"anon-{client_ip}"
        user_id = str(request.user.id) if getattr(request, "user", None) else f"anon-{client_ip}"
        endpoint = request.path
        method = request.method
        now = timezone.now()

        rules = RateLimitRule.objects.filter(is_active=True).order_by("-priority")

        most_restrictive_info = {
            "blocked": False,
            "limit": 0,
            "remaining": float("inf"),
            "reset": 0
        }

        for rule in rules:
            if rule.endpoint_pattern and not re.match(rule.endpoint_pattern, endpoint):
                continue

            limit_key = self._make_limit_key(rule.scope, api_client_id, user_id, client_ip, endpoint)
            window = rule.get_period_timedelta()
            window_start = self._get_window_start(now, window)

            block = RateLimitBlock.objects.filter(
                rule=rule,
                key=limit_key,
                blocked_until__gt=now
            ).first()
            if block:
                retry_after = int((block.blocked_until - now).total_seconds())
                return {
                    "blocked": True,
                    "limit": rule.limit,
                    "remaining": 0,
                    "reset": int(block.blocked_until.timestamp()),
                    "retry_after": retry_after
                }

            attempt, created = RateLimitAttempt.objects.get_or_create(
                rule=rule,
                key=limit_key,
                endpoint=endpoint,
                window_start=window_start,
                defaults={
                    "count": 0,
                    "method": method,
                    "last_attempt": now
                }
            )
            RateLimitAttempt.objects.filter(pk=attempt.pk).update(count=F("count") + 1)

            total_attempts = RateLimitAttempt.objects.filter(
                rule=rule,
                key=limit_key,
                window_start=window_start
            ).aggregate(total=Sum("count"))["total"] or 0

            if total_attempts > rule.limit:
                reset_time = window_start + window
                blocked_until = reset_time
                if rule.block_duration_minutes > 0:
                    extra = now + timedelta(minutes=rule.block_duration_minutes)
                    blocked_until = max(reset_time, extra)

                RateLimitBlock.objects.update_or_create(
                    rule=rule,
                    key=limit_key,
                    defaults={"blocked_until": blocked_until}
                )

                return {
                    "blocked": True,
                    "limit": rule.limit,
                    "remaining": 0,
                    "reset": int(reset_time.timestamp()),
                    "retry_after": int((blocked_until - now).total_seconds())
                }

            remaining = max(0, rule.limit - attempt.count)
            if remaining < most_restrictive_info["remaining"]:
                most_restrictive_info = {
                    "blocked": False,
                    "limit": rule.limit,
                    "remaining": remaining,
                    "reset": int((window_start + window).timestamp())
                }

        if most_restrictive_info["remaining"] == float("inf"):
            most_restrictive_info["remaining"] = -1

        return most_restrictive_info

    @staticmethod
    def _make_limit_key(scope, api_client_id, user_id, client_ip, endpoint, endpoint_pattern=None):
        if scope == "global":
            return "global"
        if scope == "api_client":
            if endpoint_pattern:
                return f"api_client{api_client_id}:endpoint:{endpoint}"
            return f"api_client:{api_client_id}"
        if scope == "user":
            if endpoint_pattern:
                return f"user:{user_id}:endpoint:{endpoint}"
            return f"user:{user_id}"
        if scope == "ip":
            if endpoint_pattern:
                return f"ip:{client_ip}:endpoint:{endpoint}"
            return f"ip:{client_ip}"
        if scope == "endpoint":
            return f"endpoint:{endpoint}"
        if scope == "user_endpoint":
            return f"user:{user_id}:endpoint:{endpoint}"
        if scope == "ip_endpoint":
            return f"ip:{client_ip}:endpoint:{endpoint}"
        return "unknown"

    @staticmethod
    def _set_headers(response, rate_limit_info=None):
        if rate_limit_info and rate_limit_info.get("limit") is not None:
            response["X-RateLimit-Limit"] = str(rate_limit_info["limit"])
            response["X-RateLimit-Remaining"] = str(rate_limit_info["remaining"])
            response["X-RateLimit-Reset"] = str(rate_limit_info["reset"])
            if rate_limit_info.get("retry_after"):
                response["Retry-After"] = str(rate_limit_info["retry_after"])
        return response

    def _save_request_log(self):
        try:
            ctx = RequestContext.get()

            path = ctx.get('request_path', '')
            if any(path.startswith(ep) for ep in self.SAVE_REQUEST_LOG_EXEMPT_PATHS):
                return

            started_at = ctx.get('started_at')
            ended_at = timezone.now()
            time_taken = (ended_at - started_at).total_seconds()

            response_data = ctx.get('response_data')
            if isinstance(response_data, dict):
                response_data = sanitize_data(response_data)

            RequestLog.objects.create(
                request_id=ctx.get('request_id'),
                api_client=ctx.get('api_client'),
                user=ctx.get('user'),
                is_authenticated=ctx.get('is_authenticated', False),
                ip_address=ctx.get('ip_address'),
                user_agent=ctx.get('user_agent', ''),
                session_key=ctx.get('session_key'),
                request_method=ctx.get('request_method'),
                request_headers=ctx.get('request_headers'),
                request_path=ctx.get('request_path'),
                request_data=ctx.get('request_data'),
                is_secure=ctx.get('is_secure', False),
                view_name=ctx.get('view_name'),
                view_args=ctx.get('view_args'),
                view_kwargs=ctx.get('view_kwargs'),
                activity_name=ctx.get('activity_name'),
                exception_type=ctx.get('exception_type'),
                exception_message=ctx.get('exception_message'),
                started_at=started_at,
                ended_at=ended_at,
                time_taken=time_taken,
                response_status=ctx.get('response_status'),
                response_headers=ctx.get('response_headers'),
                response_data=response_data,
            )
        except Exception as e:
            logger.exception(f"Failed to save RequestLog: {e}")
