"""
contracts/permissions.py

Two different worlds share this app:
  - Admin endpoints: standard DRF permission, staff only.
  - Client endpoints: NOT a standard DRF permission, because "is this
    request allowed to see THIS contract" needs the contract object
    (for the token check) before a permission class would normally get
    it. Views call get_client_authorized_contract() directly instead.

REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES is AllowAny project-wide (see
backend/settings.py) — every view in this app sets its own permission
explicitly rather than relying on that default.
"""
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission

from .models import Contract

CLIENT_TOKEN_HEADER = "HTTP_X_CONTRACT_ACCESS_TOKEN"
CLIENT_TOKEN_QUERY_PARAM = "token"


class IsContractStaff(BasePermission):
    """Admin-side endpoints — staff/superuser only."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_staff)


def _extract_token(request):
    return request.META.get(CLIENT_TOKEN_HEADER) or request.query_params.get(CLIENT_TOKEN_QUERY_PARAM) or ""


def get_client_authorized_contract(request, contract_id):
    """
    Fetches the Contract and confirms the requester is either:
      - staff (admins can always use the client-facing endpoints too,
        useful for support/testing), or
      - the linked client_user, or
      - carrying a valid, unexpired access token.
    Raises Http404 for an unknown id, PermissionDenied otherwise — never
    leaks whether a contract exists to an unauthorized caller vs. an
    outright wrong id.
    """
    try:
        contract = Contract.objects.get(pk=contract_id)
    except (Contract.DoesNotExist, ValueError, TypeError):
        raise Http404("Contract not found.")

    user = request.user if request.user and request.user.is_authenticated else None
    if user and user.is_staff:
        return contract

    token = _extract_token(request)
    if contract.has_client_access(user=user, token=token):
        return contract

    raise PermissionDenied("You don't have access to this contract.")
