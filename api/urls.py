from django.urls import include, path

app_name = "api"

urlpatterns = [path("core/", include("core.urls"))]
