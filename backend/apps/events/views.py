from django.db.models import DateField, DateTimeField, F, Func, Q, Value
from django.db.models.functions import Cast, Coalesce
from django.utils import timezone
from rest_framework import generics, serializers
from rest_framework.permissions import AllowAny

from apps.events.models import Event
from apps.sources.models import Source


class EventSerializer(serializers.ModelSerializer):
    source_name = serializers.CharField(source="source.name")
    source_url = serializers.CharField(source="source.url")

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
            "source_id",
            "source_name",
            "source_url",
            "last_seen_at",
        ]


class EventListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = EventSerializer

    def get_queryset(self):
        now = timezone.now()
        query = (
            Event.objects.filter(source__status=Source.Status.APPROVED, starts_at__isnull=False)
            .annotate(
                local_end=Cast(
                    Func(
                        F("timezone"),
                        Coalesce("ends_at", "starts_at"),
                        function="timezone",
                        output_field=DateTimeField(),
                    ),
                    DateField(),
                ),
                local_today=Cast(
                    Func(
                        F("timezone"), Value(now), function="timezone", output_field=DateTimeField()
                    ),
                    DateField(),
                ),
            )
            .filter(
                Q(ends_at__gte=now)
                | Q(ends_at__isnull=True, starts_at__gte=now)
                | Q(is_all_day=True, local_end__gte=F("local_today"))
            )
            .select_related("source")
        )
        location = self.request.query_params.get("location", "").strip()
        if location:
            query = query.filter(Q(address__icontains=location) | Q(venue_name__icontains=location))
        mode = self.request.query_params.get("attendance_mode")
        if mode:
            if mode not in Event.AttendanceMode.values:
                raise serializers.ValidationError(
                    {"attendance_mode": "Use online, offline or mixed"}
                )
            query = query.filter(attendance_mode=mode)
        return query.order_by("starts_at", "id")
