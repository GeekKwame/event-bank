"""Public, read-only events API."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import OuterRef, Q, QuerySet, Subquery
from django.utils import timezone
from rest_framework import serializers
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny

from apps.events.models import Event
from apps.ingestion.models import SourceRun
from apps.sources.models import Source
from connectors.registry import UnsupportedSourceError, get_connector

ONLINE_MODES = (Event.AttendanceMode.ONLINE, Event.AttendanceMode.MIXED)
IN_PERSON_MODES = (Event.AttendanceMode.OFFLINE, Event.AttendanceMode.MIXED)
MANUAL_PLATFORM = "Verified submission"


class EventPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class EventFilterSerializer(serializers.Serializer):
    when = serializers.ChoiceField(choices=["upcoming", "past", "all"], default="upcoming")
    starts_after = serializers.DateField(required=False)
    starts_before = serializers.DateField(required=False)
    location = serializers.CharField(required=False, max_length=100, trim_whitespace=True)
    attendance = serializers.ChoiceField(choices=["online", "in_person"], required=False)
    include_cancelled = serializers.BooleanField(default=False)
    source = serializers.IntegerField(required=False, min_value=1)


def _platform_name(connector: str) -> str:
    if connector == "manual":
        return MANUAL_PLATFORM
    try:
        return get_connector(connector).name
    except UnsupportedSourceError:
        return connector


class SourceSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="source.pk")
    name = serializers.SerializerMethodField()
    platform = serializers.SerializerMethodField()
    url = serializers.URLField(source="source.url")
    last_checked_at = serializers.DateTimeField(source="source_last_checked_at", allow_null=True)

    def get_name(self, event) -> str:
        return str(event.source)

    def get_platform(self, event) -> str:
        return _platform_name(event.source.connector)


class EventSerializer(serializers.ModelSerializer):
    source = SourceSerializer(source="*")

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
            "organizer_name",
            "image_url",
            "description",
            "first_seen_at",
            "last_seen_at",
            "last_changed_at",
            "source",
        ]
        read_only_fields = fields


def published_events() -> QuerySet[Event]:
    """Events from approved sources, annotated with each source's last successful run."""
    last_success = (
        SourceRun.objects.filter(source=OuterRef("source"), status=SourceRun.Status.SUCCEEDED)
        .order_by("-finished_at")
        .values("finished_at")[:1]
    )
    return (
        Event.objects.filter(source__status=Source.Status.APPROVED)
        .select_related("source")
        .annotate(source_last_checked_at=Subquery(last_success))
    )


def _start_of_day(day) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.TIME_ZONE))


class PublicReadOnlyMixin:
    authentication_classes = []
    permission_classes = [AllowAny]
    serializer_class = EventSerializer


class EventListView(PublicReadOnlyMixin, ListAPIView):
    """List events. Defaults to upcoming, non-cancelled events, soonest first.

    Query parameters: when=upcoming|past|all, starts_after / starts_before (YYYY-MM-DD,
    inclusive, in the server timezone), location (matches venue or address),
    attendance=online|in_person, include_cancelled=true, source=<id>, page, page_size.
    """

    pagination_class = EventPagination

    def get_queryset(self):
        params = EventFilterSerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        filters = params.validated_data
        now = timezone.now()
        events = published_events().exclude(starts_at__isnull=True)

        not_ended = Q(ends_at__gte=now) | Q(ends_at__isnull=True, starts_at__gte=now)
        if filters["when"] == "upcoming":
            events = events.filter(not_ended)
            ordering = ["starts_at", "id"]
        elif filters["when"] == "past":
            events = events.exclude(not_ended)
            ordering = ["-starts_at", "-id"]
        else:
            ordering = ["starts_at", "id"]

        if "starts_after" in filters:
            events = events.filter(starts_at__gte=_start_of_day(filters["starts_after"]))
        if "starts_before" in filters:
            day_after = _start_of_day(filters["starts_before"]) + timedelta(days=1)
            events = events.filter(starts_at__lt=day_after)
        if location := filters.get("location"):
            events = events.filter(
                Q(venue_name__icontains=location) | Q(address__icontains=location)
            )
        if filters.get("attendance") == "online":
            events = events.filter(attendance_mode__in=ONLINE_MODES)
        elif filters.get("attendance") == "in_person":
            events = events.filter(attendance_mode__in=IN_PERSON_MODES)
        if not filters["include_cancelled"]:
            events = events.exclude(status=Event.Status.CANCELLED)
        if "source" in filters:
            events = events.filter(source_id=filters["source"])
        return events.order_by(*ordering)


class EventDetailView(PublicReadOnlyMixin, RetrieveAPIView):
    def get_queryset(self):
        return published_events()
