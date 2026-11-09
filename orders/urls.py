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
