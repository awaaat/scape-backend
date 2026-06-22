from django.urls import path

from .views import TrackPageView

urlpatterns = [
    path("track-visit/", TrackPageView.as_view(), name="track-visit"),
]
