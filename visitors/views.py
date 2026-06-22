from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .serializers import PageViewSerializer


class TrackPageView(APIView):
    """
    POST /api/track-visit/
    Call this from the React app on every route change to log page views
    against the current visitor session (created automatically by the
    VisitorTrackingMiddleware).
    """

    throttle_scope = "track"

    def post(self, request):
        visitor = getattr(request, "visitor", None)
        if visitor is None:
            return Response({"error": "No active session."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = PageViewSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(visitor=visitor)
            return Response({"status": "tracked"}, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
