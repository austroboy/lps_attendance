from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.templatetags.static import static as static_url
from django.urls import include, path
from django.views.generic.base import RedirectView

from devices.views import device_endpoint

urlpatterns = [
    # The terminals. Keep these first and keep them un-namespaced — the device
    # firmware appends nothing and expects a plain path.
    path("ebkn/", device_endpoint, name="ebkn_endpoint"),
    path("ebkn", device_endpoint),

    # Browsers ask for this whether or not you link one; answer it instead of
    # filling the log with 404s while you are watching for device traffic.
    path("favicon.ico", RedirectView.as_view(url=static_url("img/lps-logo-64.png"), permanent=False)),

    path("django-admin/", admin.site.urls),
    path("devices/", include("devices.urls")),
    path("academics/", include("academics.urls")),
    path("attendance/", include("attendance.urls")),
    path("sms/", include("smsapp.urls")),
    path("reports/", include("reports.urls")),
    path("", include("accounts.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
