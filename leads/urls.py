from django.urls import path

from .views import ContactView, ExitIntentView

urlpatterns = [
    path("contact/", ContactView.as_view(), name="contact"),
    path("exit-intent/", ExitIntentView.as_view(), name="exit-intent"),
]