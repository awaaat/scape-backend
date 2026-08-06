"""
projects/pagination.py

REST_FRAMEWORK has no DEFAULT_PAGINATION_CLASS project-wide (see
backend/settings.py) — every other app in this codebase returns
unpaginated lists too, so changing that globally would be a
project-wide behavior change well outside this app's scope. Instead,
this app opts itself into pagination locally, since project/admin
lists are exactly the kind of thing that grows unbounded over time.
"""
from rest_framework.pagination import PageNumberPagination


class ProjectResultsPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
