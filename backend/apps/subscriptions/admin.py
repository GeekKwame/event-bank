from django.contrib import admin

from apps.subscriptions.models import SourceRequest, Subscriber, Subscription


@admin.register(Subscriber, Subscription, SourceRequest)
class SubscriptionAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
