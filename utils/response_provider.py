from django.core.exceptions import ValidationError, ObjectDoesNotExist, PermissionDenied
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse

from core.services.payment_services import PaymentError


class ResponseProvider:
    @staticmethod
    def _response(
            success: bool,
            message: str,
            status: int,
            data=None,
            error=None
    ) -> JsonResponse:
        payload = {
            'success': success,
            'message': message,
            'data': data or {},
            'error': error or '',
        }
        return JsonResponse(payload, status=status, encoder=DjangoJSONEncoder)

    @classmethod
    def handle_exception(cls, ex: Exception) -> JsonResponse:
        if isinstance(ex, ValidationError):
            if hasattr(ex, "messages"):
                error_message = ", ".join(ex.messages)
            else:
                error_message = str(ex)
            return cls.bad_request(message="Validation Error", error=error_message)
        elif isinstance(ex, PaymentError):
            return cls.bad_request(message="Payment Error", error=str(ex))
        elif isinstance(ex, ObjectDoesNotExist):
            return cls.not_found(error=str(ex))
        elif isinstance(ex, PermissionDenied):
            return cls.forbidden(error=str(ex))
        else:
            return cls.server_error(error=str(ex))

    @classmethod
    def success(cls, message='Success', data=None):
        return cls._response(True, message, 200, data=data)

    @classmethod
    def created(cls, message='Created', data=None):
        return cls._response(True, message, 201, data=data)

    @classmethod
    def accepted(cls, message='Accepted', data=None):
        return cls._response(True, message, 202, data=data)

    @classmethod
    def bad_request(cls, message='Bad Request', error=None):
        return cls._response(False, message, 400, error=error)

    @classmethod
    def unauthorized(cls, message='Unauthorized', error=None):
        return cls._response(False, message, 401, error=error)

    @classmethod
    def forbidden(cls, message='Forbidden', error=None):
        return cls._response(False, message, 403, error=error)

    @classmethod
    def not_found(cls, message='Resource Not Found', error=None):
        return cls._response(False, message, 404, error=error)

    @classmethod
    def too_many_requests(cls, message='Rate Limit Exceeded', error=None):
        return cls._response(False, message, 429, error=error)

    @classmethod
    def server_error(cls, message='Internal Server Error', error=None):
        return cls._response(False, message, 500, error=error)

    @classmethod
    def not_implemented(cls, message='Not Implemented', error=None):
        return cls._response(False, message, 501, error=error)

    @classmethod
    def service_unavailable(cls, message='Service Unavailable', error=None):
        return cls._response(False, message, 503, error=error)
