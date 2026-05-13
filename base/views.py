from django.http import JsonResponse


def healthz(request):
    return JsonResponse({"success": True, "message": "healthy"})
