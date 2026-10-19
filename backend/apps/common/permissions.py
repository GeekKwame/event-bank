from rest_framework.permissions import BasePermission


class DenyAll(BasePermission):
    """Default for views that have not declared permissions, so they fail closed."""

    def has_permission(self, request, view):
        return False
