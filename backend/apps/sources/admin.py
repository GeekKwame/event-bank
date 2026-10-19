from django.contrib import admin

from apps.sources.models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ["id", "__str__", "connector", "status", "timezone", "created_at", "reviewed_at"]
    list_filter = ["status", "connector"]
    search_fields = ["url", "name"]
    readonly_fields = [
        "url",
        "connector",
        "submitted_by",
        "reviewed_by",
        "reviewed_at",
        "created_at",
        "updated_at",
    ]
    actions = ["approve", "reject", "pause"]

    def has_add_permission(self, request):
        # Sources must go through register_source so URL validation always runs.
        return False

    def _review(self, request, queryset, status):
        for source in queryset:
            source.review(status, reviewer=request.user)
        self.message_user(request, f"{queryset.count()} source(s) marked {status}.")

    @admin.action(description="Approve selected sources")
    def approve(self, request, queryset):
        self._review(request, queryset, Source.Status.APPROVED)

    @admin.action(description="Reject selected sources")
    def reject(self, request, queryset):
        self._review(request, queryset, Source.Status.REJECTED)

    @admin.action(description="Pause selected sources")
    def pause(self, request, queryset):
        self._review(request, queryset, Source.Status.PAUSED)
