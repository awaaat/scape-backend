import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.cache import patch_vary_headers
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import catalog, services
from .models import Order, OrderFile
from .serializers import OrderFileSerializer, OrderSerializer, OrderWriteSerializer
from .storage import StorageUploadFailed, delete_order_file, upload_order_file

logger = logging.getLogger("orders")

SUCCESS_MESSAGE = "Order received. We'll email your quote within 24 hours."


class PublicView(APIView):
    """Public, token-based. No JWT (a stale token in the header must never 401
    a public order) and ONLY scoped throttles — the global 30/min per-IP anon
    throttle would choke a multi-step wizard on shared campus wifi."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]


def _site_from_request(request):
    """Site for endpoints that have no order yet (catalog, start, quick)."""
    data = request.data if request.method != "GET" else {}
    explicit = data.get("site") if hasattr(data, "get") else None
    return catalog.resolve_site(request, explicit if isinstance(explicit, str) else None)


def _writer(request, site):
    return OrderWriteSerializer(data=request.data, context={"site": site})


def _envelope(order, **extra):
    return {"token": order.public_token, "reference": order.reference,
            "order": OrderSerializer(order).data, **extra}


def _fake_ok(code=status.HTTP_201_CREATED):
    """Honeypot hit: look successful, store nothing."""
    return Response({"token": "x", "reference": "SCP-000000", "message": SUCCESS_MESSAGE}, status=code)


def _editable_or_409(order):
    if order.is_editable:
        return None
    return Response(
        {"detail": "This order has already been submitted.", "reference": order.reference},
        status=status.HTTP_409_CONFLICT,
    )


class CatalogView(PublicView):
    """GET /api/orders/catalog/ — wizard config (steps, options, limits)."""
    throttle_scope = "order_read"

    def get(self, request):
        resp = Response(catalog.public_catalog(_site_from_request(request)))
        resp["Cache-Control"] = "public, max-age=300"
        patch_vary_headers(resp, ["Origin", "Referer"])  # config differs per calling site
        return resp


class OrderStartView(PublicView):
    """POST /api/orders/start/ — create a draft (call on the first answered step)."""
    throttle_scope = "order_start"

    def post(self, request):
        ser = _writer(request, _site_from_request(request))
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get("website"):
            return _fake_ok()
        order = services.create_order(ser.validated_data, request)
        return Response(_envelope(order), status=status.HTTP_201_CREATED)


class OrderDetailView(PublicView):
    """GET  /api/orders/<token>/  — resume a draft.
       PATCH /api/orders/<token>/ — save the current step."""
    throttle_scope = "order_save"

    def get(self, request, token):
        order = get_object_or_404(Order, public_token=token)
        return Response(OrderSerializer(order).data)

    def patch(self, request, token):
        order = get_object_or_404(Order, public_token=token)
        blocked = _editable_or_409(order)
        if blocked:
            return blocked
        ser = _writer(request, order.site_info)
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get("website"):
            return _fake_ok(status.HTTP_200_OK)
        order = services.apply_fields(order, ser.validated_data)
        return Response(OrderSerializer(order).data)


class OrderSubmitView(PublicView):
    """POST /api/orders/<token>/submit/ — finalise. Idempotent: a double-click
    or retry returns the same reference and never notifies twice. The body may
    carry the last step's fields so the final answers + submit are one call."""
    throttle_scope = "order_submit"

    def post(self, request, token):
        order = get_object_or_404(Order, public_token=token)
        ser = _writer(request, order.site_info)
        ser.is_valid(raise_exception=True)
        if ser.validated_data.get("website"):
            return _fake_ok(status.HTTP_200_OK)

        if not order.is_editable:
            return Response({"reference": order.reference, "already_submitted": True,
                             "message": SUCCESS_MESSAGE})

        order = services.apply_fields(order, ser.validated_data)
        errors = services.order_missing_fields(order)
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        order, first_time = services.submit_order(order)
        return Response({"reference": order.reference, "already_submitted": not first_time,
                         "message": SUCCESS_MESSAGE, "order": OrderSerializer(order).data})


class OrderQuickView(PublicView):
    """POST /api/orders/quick/ — one-shot order (inline forms on landing pages,
    exit-intent, etc.). Creates AND submits in a single call."""
    throttle_scope = "order_submit"

    def post(self, request):
        site = _site_from_request(request)
        ser = _writer(request, site)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        if data.get("website"):
            return _fake_ok()
        flat = {**data, **data.get("answers", {})}
        errors = services.missing_fields(flat, site["required"])
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        order = services.create_order(data, request)
        order, _ = services.submit_order(order)
        return Response({"reference": order.reference, "message": SUCCESS_MESSAGE},
                        status=status.HTTP_201_CREATED)


class OrderFileUploadView(PublicView):
    """POST /api/orders/<token>/files/ (multipart, field name: file)."""
    throttle_scope = "order_upload"
    parser_classes = [MultiPartParser]

    def post(self, request, token):
        order = get_object_or_404(Order, public_token=token)
        blocked = _editable_or_409(order)
        if blocked:
            return blocked

        f = request.FILES.get("file")
        if f is None:
            return Response({"file": ["No file was uploaded."]}, status=status.HTTP_400_BAD_REQUEST)

        ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
        if ext not in catalog.ALLOWED_EXTENSIONS:
            return Response({"file": [f"'.{ext or '?'}' files aren't accepted. Zip it if you need to."]},
                            status=status.HTTP_400_BAD_REQUEST)
        max_mb = getattr(settings, "ORDER_FILE_MAX_MB", 25)
        if f.size > max_mb * 1024 * 1024:
            return Response({"file": [f"File is too large (max {max_mb} MB)."]},
                            status=status.HTTP_400_BAD_REQUEST)
        if order.files.count() >= getattr(settings, "ORDER_MAX_FILES", 10):
            return Response({"file": ["Too many files on this order."]}, status=status.HTTP_400_BAD_REQUEST)

        try:
            url, path = upload_order_file(f, order.reference)
        except StorageUploadFailed as exc:
            logger.error("Order %s upload failed: %s", order.reference, exc)
            return Response({"file": ["Upload failed. Please try again, or email the file to us."]},
                            status=status.HTTP_502_BAD_GATEWAY)

        of = OrderFile.objects.create(
            order=order, original_name=f.name[:255], url=url, storage_path=path,
            size_bytes=f.size, content_type=(f.content_type or "")[:120],
        )
        services.touch(order)
        return Response(OrderFileSerializer(of).data, status=status.HTTP_201_CREATED)


class OrderFileDeleteView(PublicView):
    """DELETE /api/orders/<token>/files/<id>/"""
    throttle_scope = "order_save"

    def delete(self, request, token, file_id):
        order = get_object_or_404(Order, public_token=token)
        blocked = _editable_or_409(order)
        if blocked:
            return blocked
        of = get_object_or_404(OrderFile, pk=file_id, order=order)
        delete_order_file(of.storage_path)
        of.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
