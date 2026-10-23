from django.contrib import admin
from django.urls import path

from apps.common.views import HealthView
from apps.events.api import EventDetailView, EventListView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/events/", EventListView.as_view(), name="event-list"),
    path("api/events/<int:pk>/", EventDetailView.as_view(), name="event-detail"),
]
