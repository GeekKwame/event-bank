from datetime import timedelta

from django.utils import timezone
from rest_framework import generics, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.sources.models import Source
from apps.sources.services import SourceRegistrationError, register_source
from connectors.registry import all_connectors


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = ["id", "name", "url", "connector", "status", "timezone"]


class SourceSubmissionSerializer(serializers.Serializer):
    url = serializers.URLField(max_length=2000)
    name = serializers.CharField(max_length=200, required=False, default="")
    timezone = serializers.CharField(max_length=64, required=False)

    def create(self, validated_data):
        user = self.context["request"].user
        if (
            Source.objects.filter(
                submitted_by=user, created_at__gte=timezone.now() - timedelta(days=1)
            ).count()
            >= 5
        ):
            raise serializers.ValidationError(
                {"code": "submission_limit", "detail": "Five sources per day"}
            )
        try:
            return register_source(**validated_data, submitted_by=user)
        except SourceRegistrationError as exc:
            raise serializers.ValidationError({"code": exc.code, "detail": str(exc)}) from None


class SourceListView(generics.ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = SourceSerializer
    queryset = Source.objects.filter(status=Source.Status.APPROVED).order_by("pk")


class SourceSubmitView(generics.CreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SourceSubmissionSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = serializer.save()
        return Response(SourceSerializer(source).data, status=201)


class PlatformListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(
            [
                {"slug": cls.slug, "name": cls.name, "hosts": sorted(cls.source_url_hosts())}
                for cls in all_connectors().values()
            ]
        )
