"""
agreements/permissions.py

Same split as contracts/permissions.py, deliberately mirrored rather
than shared (see agreements/models.py's module docstring on why this
app duplicates contracts' security pattern fresh instead of importing
from it): two different worlds share this app.

  - Admin endpoints: standard DRF permission, staff only.
  - Client endpoints: NOT a standard DRF permission, because "is this
    request allowed to see THIS agreement" needs the Agreement object
    (for the token check) before a permission class would normally get
    it — DRF permission classes run before the view has looked up the
    object being acted on. Views call get_client_authorized_agreement()
    directly instead, the same way contracts.views already does for
    Contract.

REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES is AllowAny project-wide (see
backend/settings.py) — every view in this app sets its own permission
explicitly rather than relying on that default.
"""
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import Agreement

CLIENT_TOKEN_HEADER = "HTTP_X_AGREEMENT_ACCESS_TOKEN"
CLIENT_TOKEN_QUERY_PARAM = "token"


class IsAgreementStaff(BasePermission):
    """Admin-side endpoints — staff/superuser only. Covers everything
    that creates, sends, voids, or countersigns an Agreement, and all
    of AgreementTemplate's CRUD/versioning endpoints."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


def _extract_token(request):
    return request.META.get(CLIENT_TOKEN_HEADER) or request.query_params.get(CLIENT_TOKEN_QUERY_PARAM) or ""


def get_client_authorized_agreement(request, agreement_id):
    """
    Fetches the Agreement and confirms the requester is either:
      - staff (admins can always use the client-facing endpoints too,
        useful for support/testing — e.g. confirming what a client
        actually sees before a signing call), or
      - the linked client_user, or
      - carrying a valid, unexpired access token.

    Raises Http404 for an unknown id, PermissionDenied otherwise —
    never leaks whether an agreement exists to an unauthorized caller
    vs. an outright wrong id, same reasoning as
    contracts.get_client_authorized_contract.
    """
    try:
        agreement = Agreement.objects.get(pk=agreement_id)
    except (Agreement.DoesNotExist, ValueError, TypeError):
        raise Http404("Agreement not found.")

    user = request.user if request.user and request.user.is_authenticated else None
    if user and user.is_staff:
        return agreement

    token = _extract_token(request)
    if agreement.has_client_access(user=user, token=token):
        return agreement

    raise PermissionDenied("You don't have access to this agreement.")