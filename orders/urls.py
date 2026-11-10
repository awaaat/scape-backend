from django.urls import path

from . import views

# Literal routes first — <str:token> would otherwise swallow "catalog", "start", "quick".
urlpatterns = [
    path("catalog/", views.CatalogView.as_view(), name="orders-catalog"),
    path("start/", views.OrderStartView.as_view(), name="orders-start"),
    path("quick/", views.OrderQuickView.as_view(), name="orders-quick"),
    path("<str:token>/", views.OrderDetailView.as_view(), name="orders-detail"),
    path("<str:token>/submit/", views.OrderSubmitView.as_view(), name="orders-submit"),
    path("<str:token>/files/", views.OrderFileUploadView.as_view(), name="orders-files"),
    path("<str:token>/files/<int:file_id>/", views.OrderFileDeleteView.as_view(), name="orders-file-delete"),
]


# ═══ ORDER ENGAGEMENT LAYER (install_order_engagement.sh) ═══
# Appended after the literal routes above. All of these are two-segment paths, so
# the single-segment "<str:token>/" route can never swallow them.
from . import engagement_views as _engagement_views  # noqa: E402

urlpatterns += [
    path("unsubscribe/<str:signed>/", _engagement_views.OrderUnsubscribeView.as_view(), name="orders-unsubscribe"),
    path("<str:token>/resume/", _engagement_views.OrderResumeRedirectView.as_view(), name="orders-resume"),
    path("<str:token>/leave/", _engagement_views.OrderLeaveView.as_view(), name="orders-leave"),
    path("<str:token>/return/", _engagement_views.OrderReturnView.as_view(), name="orders-return"),
]
