import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt


# Create your views here.
@csrf_exempt
def test(request):
    data = json.loads(request.body)
    print(data)
    return JsonResponse({"status": "success"})
