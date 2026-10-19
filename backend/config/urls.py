from django.contrib import admin
from django.urls import path

from apps.common.views import HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
]
