from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse


def health_check(request):
    return JsonResponse({"status": "ok"})


def api_root(request):
    return JsonResponse({
        "status": "ok",
        "endpoints": [
            "/api/health/",
            "/api/contact/",
            "/api/exit-intent/",
            "/api/track-visit/",
            "/api/jobs/",
        ],
    })


urlpatterns = [
    path("admin/", admin.site.urls),
    path("", api_root, name="root"),
    path("api/", api_root, name="api-root"),
    path("api/health/", health_check, name="health"),
    path("api/", include("leads.urls")),
    path("api/", include("visitors.urls")),
    path("api/", include("jobs.urls")),
    path("api/", include("property_intel.urls")),   # add this
    path("api/", include("payments.urls")),
    path("api/users/", include("users.urls")),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)