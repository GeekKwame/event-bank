from django.contrib import admin

from apps.events.models import Event, EventRevision


class ReadOnlyAdmin(admin.ModelAdmin):
    """Events are owned by ingestion; editing them in the admin would break change detection."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EventRevisionInline(admin.TabularInline):
    model = EventRevision
    extra = 0
    fields = ["created_at", "kind", "changed_fields", "is_significant", "source_run"]
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Event)
class EventAdmin(ReadOnlyAdmin):
    list_display = ["title", "source", "starts_at", "status", "last_seen_at", "last_changed_at"]
    list_filter = ["status", "attendance_mode", "source"]
    search_fields = ["title", "external_id", "venue_name"]
    inlines = [EventRevisionInline]


@admin.register(EventRevision)
class EventRevisionAdmin(ReadOnlyAdmin):
    list_display = ["event", "kind", "is_significant", "created_at"]
    list_filter = ["kind", "is_significant"]
