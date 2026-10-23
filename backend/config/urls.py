from django.contrib import admin
from django.urls import path

from apps.common.views import HealthView
from apps.events.views import EventDetailView, EventListView
from apps.sources.views import PlatformListView, SourceListView, SourceSubmitView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/events/", EventListView.as_view(), name="events"),
    path("api/events/<int:pk>/", EventDetailView.as_view(), name="event-detail"),
    path("api/sources/", SourceListView.as_view(), name="sources"),
    path("api/sources/submit/", SourceSubmitView.as_view(), name="source-submit"),
    path("api/platforms/", PlatformListView.as_view(), name="platforms"),
]
