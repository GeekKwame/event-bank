"""Public, read-only events API."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import DateField, DateTimeField, F, Func, OuterRef, Q, Subquery, Value
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from rest_framework import generics, serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny

from apps.events.models import Event
from apps.ingestion.models import SourceRun
from apps.ingestion.services import MANUAL_CONNECTOR
from apps.sources.models import Source
from connectors.registry import UnsupportedSourceError, get_connector

ONLINE_MODES = (Event.AttendanceMode.ONLINE, Event.AttendanceMode.MIXED)
IN_PERSON_MODES = (Event.AttendanceMode.OFFLINE, Event.AttendanceMode.MIXED)
MANUAL_PLATFORM = "Verified submission"


def platform_name(connector: str) -> str:
    if connector == MANUAL_CONNECTOR:
        return MANUAL_PLATFORM
    try:
        return get_connector(connector).name
    except UnsupportedSourceError:
        return connector


class EventPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class EventFilterSerializer(serializers.Serializer):
    when = serializers.ChoiceField(choices=["upcoming", "past", "all"], default="upcoming")
    starts_after = serializers.DateField(required=False)
    starts_before = serializers.DateField(required=False)
    location = serializers.CharField(required=False, max_length=100)
    attendance = serializers.ChoiceField(choices=["online", "in_person"], required=False)
    attendance_mode = serializers.ChoiceField(choices=Event.AttendanceMode.values, required=False)
    include_cancelled = serializers.BooleanField(default=False)
    source = serializers.IntegerField(required=False, min_value=1)


class EventSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name")
    source_url = serializers.CharField(source="source.url")
    source_platform = serializers.SerializerMethodField()
    source_last_checked_at = serializers.DateTimeField(allow_null=True)

    class Meta:
        model = Event
        fields = [
            "id",
            "title",
            "url",
            "starts_at",
            "ends_at",
            "is_all_day",
            "timezone",
            "venue_name",
            "address",
            "attendance_mode",
            "status",
            "description",
            "organizer_name",
            "image_url",
            "attribution",
            "source_id",
            "source_name",
            "source_url",
            "source_platform",
            "source_last_checked_at",
            "first_seen_at",
            "last_seen_at",
            "last_changed_at",
        ]
        read_only_fields = fields

    def get_source_platform(self, event) -> str:
        return platform_name(event.source.connector)


def published_events():
    """Dated events from approved sources, with each source's last successful check."""
    last_success = (
        SourceRun.objects.filter(source=OuterRef("source"), status=SourceRun.Status.SUCCEEDED)
        .order_by("-finished_at")
        .values("finished_at")[:1]
    )
    return (
        Event.objects.filter(source__status=Source.Status.APPROVED, starts_at__isnull=False)
        .select_related("source")
        .annotate(source_last_checked_at=Subquery(last_success))
    )


def _not_ended(now):
    """Upcoming or ongoing. All-day events count until the end of their final local date."""
    local_date = {"function": "timezone", "output_field": DateTimeField()}
    return (
        Q(ends_at__gte=now)
        | Q(ends_at__isnull=True, starts_at__gte=now)
        | Q(
            is_all_day=True,
            local_end__gte=Cast(Func(F("timezone"), Value(now), **local_date), DateField()),
        )
    )


def _start_of_day(day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.TIME_ZONE))


class PublicReadOnlyMixin:
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = EventSerializer


class EventListView(PublicReadOnlyMixin, generics.ListAPIView):
    """Upcoming, non-cancelled events by default, soonest first. See the README for filters."""

    pagination_class = EventPagination

    def get_queryset(self):
        params = EventFilterSerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        filters = params.validated_data
        now = timezone.now()
        local_end = Cast(
            Func(
                F("timezone"),
                Coalesce("ends_at", "starts_at"),
                function="timezone",
                output_field=DateTimeField(),
            ),
            DateField(),
        )
        events = published_events().annotate(local_end=local_end)

        if filters["when"] == "upcoming":
            events = events.filter(_not_ended(now))
            ordering = ["starts_at", "id"]
        elif filters["when"] == "past":
            events = events.exclude(_not_ended(now))
            ordering = ["-starts_at", "-id"]
        else:
            ordering = ["starts_at", "id"]

        if "starts_after" in filters:
            events = events.filter(starts_at__gte=_start_of_day(filters["starts_after"]))
        if "starts_before" in filters:
            day_after = _start_of_day(filters["starts_before"]) + timedelta(days=1)
            events = events.filter(starts_at__lt=day_after)
        if location := filters.get("location", "").strip():
            events = events.filter(
                Q(address__icontains=location) | Q(venue_name__icontains=location)
            )
        if filters.get("attendance") == "online":
            events = events.filter(attendance_mode__in=ONLINE_MODES)
        elif filters.get("attendance") == "in_person":
            events = events.filter(attendance_mode__in=IN_PERSON_MODES)
        if "attendance_mode" in filters:
            events = events.filter(attendance_mode=filters["attendance_mode"])
        if not filters["include_cancelled"]:
            events = events.exclude(status=Event.Status.CANCELLED)
        if "source" in filters:
            events = events.filter(source_id=filters["source"])
        return events.order_by(*ordering)


class EventDetailView(PublicReadOnlyMixin, generics.RetrieveAPIView):
    def get_queryset(self):
        return published_events()
