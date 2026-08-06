"""
projects/permissions.py

Two different worlds share this app, same split as contracts/permissions.py:
  - Admin endpoints (/api/projects/admin/...): standard DRF permission,
    staff only.
  - Client endpoints (/api/projects/...): "is this request allowed to
    see THIS project" needs the object itself, so views call
    get_owned_project() directly rather than relying on a permission
    class that never sees the object.

Unlike contracts, there's no no-login access-token path here — every
project belongs to an authenticated client account, so ownership is
just request.user == project.owner (or staff).

REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES is AllowAny project-wide (see
backend/settings.py) — every view in this app sets its own permission
explicitly rather than relying on that default.
"""
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import Project


class IsProjectStaff(BasePermission):
    """Admin-side endpoints — staff/superuser only."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


def get_owned_project(request, project_id):
    """
    Fetches the Project and confirms the requester is either staff
    (admins can always use the client-facing endpoints too, useful for
    support/testing) or the project's own owner. Raises Http404 for an
    unknown id, PermissionDenied otherwise — never leaks whether a
    project exists to an unauthorized caller vs. an outright wrong id.
    """
    try:
        project = Project.objects.get(pk=project_id)
    except (Project.DoesNotExist, ValueError, TypeError):
        raise Http404("Project not found.")

    user = request.user if request.user and request.user.is_authenticated else None
    if user and (user.is_staff or project.owner_id == user.id):
        return project

    raise PermissionDenied("You don't have access to this project.")
