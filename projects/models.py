"""
projects/models.py

Client-submitted work requests, plus a live message thread per project
between the client (owner) and admin staff. Modeled directly on
contracts/models.py's Contract + Message pair, minus the no-login
access-token dance — a project always belongs to an authenticated
client (users.UserSignup / the auth User), so there's no equivalent of
Contract.access_token_hash here. Auth is just "are you staff, or are
you project.owner" (see permissions.py).

Real-time delivery mirrors contracts exactly: every ProjectMessage
creation goes through services.post_project_message(), and
signals.broadcast_new_project_message (a post_save receiver) is the
single place that pushes it out over the channel layer to
project_<id> — regardless of whether the message came in over REST,
the websocket consumer, or the Django admin.

── Sync with contracts/milestones/payments ─────────────────────────
A Project is deliberately NOT a second place where money/milestones
live. It's the pre-sale intake + negotiation-scoping thread. Once an
admin is ready to formalize a deal, services.convert_project_to_contract()
creates a real contracts.Contract from this project's data — from that
point on, ALL milestone creation, invoicing, and Paystack payment flow
through the existing contracts app exactly as it already does today.
`contract` below is the one-way link recording that a project became a
contract; it does not duplicate any contract/milestone/payment logic.

── Attachments ───────────────────────────────────────────────────────
Stored in Supabase Storage (see storage.py), NOT Django's local
FileField/MEDIA_ROOT — Render's filesystem is ephemeral and wipes
uploads on every deploy/restart, same reasoning property_intel already
applied to its own uploads. attachment_url is the one field of record;
attachment_filename is kept only so the UI can show the client's
original filename instead of a UUID.

── Audit trail ────────────────────────────────────────────────────────
ProjectStatusChange is an append-only log of every status transition —
who changed it, from what, to what, when, and an optional note. Written
by services.change_project_status(), the only place project.status is
ever allowed to change (see services.py) — this is what makes "who
moved this to cancelled and when" answerable in a support dispute
instead of only visible as an updated_at timestamp with no history.
"""
import uuid

from django.conf import settings
from django.db import models

PROJECT_STATUS_CHOICES = [
    ("submitted", "Submitted"),
    ("under_review", "Under review"),
    ("in_progress", "In progress"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]

# Statuses in which the client is still allowed to edit their own
# submission (see Project.is_client_editable / views.ProjectDetailView).
# Once an admin has started actual work, the brief is locked — further
# changes should go through the message thread instead.
CLIENT_EDITABLE_STATUSES = {"submitted", "under_review"}

# Explicit allow-list of status transitions — prevents an admin fat-
# fingering a project straight from "completed" back to "submitted", or
# resurrecting a "cancelled" one silently. Checked in
# Project.can_transition_to() / services.change_project_status().
ALLOWED_STATUS_TRANSITIONS = {
    "submitted": {"under_review", "in_progress", "cancelled"},
    "under_review": {"submitted", "in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),   # terminal — reopen by creating a new project instead
    "cancelled": set(),   # terminal
}

MESSAGE_SENDER_TYPE_CHOICES = [
    ("admin", "Admin"),
    ("client", "Client"),
]

ALLOWED_ATTACHMENT_EXTENSIONS = {
    "pdf", "doc", "docx", "txt", "rtf",
    "png", "jpg", "jpeg", "webp",
    "xls", "xlsx", "csv",
    "zip",
}


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects",
        help_text="The client who submitted this project.",
    )

    title = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(
        max_length=100, blank=True,
        help_text="Free-text project type, e.g. 'Data collection', 'Web development'.",
    )
    # Kept in sync with the frontend's currency <select> options
    # (NewProjectForm.jsx) — anything outside this set is rejected both
    # here (DB constraint, last line of defense) and in
    # ProjectCreateSerializer/ProjectUpdateSerializer (clean 400 instead
    # of a raw IntegrityError).
    CURRENCY_CHOICES = [
        ("KES", "KES"),
        ("USD", "USD"),
        ("GBP", "GBP"),
        ("EUR", "EUR"),
    ]

    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default="KES")
    deadline = models.DateField(null=True, blank=True)

    # See storage.py — Supabase-backed, not local disk. attachment_url is
    # the field of record; attachment_filename/content_type are display
    # metadata only.
    attachment_url = models.URLField(max_length=500, blank=True)
    attachment_filename = models.CharField(max_length=255, blank=True)
    attachment_content_type = models.CharField(max_length=100, blank=True)

    status = models.CharField(
        max_length=20, choices=PROJECT_STATUS_CHOICES, default="submitted", db_index=True,
    )

    # Optional — set by an admin once someone is actually handling this
    # project. Left null until then; SET_NULL so removing a staff
    # account never deletes project history.
    assigned_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_projects",
    )

    # Set once services.convert_project_to_contract() has been called —
    # see module docstring. SET_NULL (not CASCADE) so deleting a
    # Contract for some administrative reason never takes the original
    # project intake record down with it.
    contract = models.OneToOneField(
        "contracts.Contract", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="source_project",
        help_text="Set once this project has been formalized into a Contract "
                   "(see services.convert_project_to_contract). Milestones and "
                   "payments live on the Contract from that point on, not here.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(budget__isnull=True) | models.Q(budget__gte=0),
                name="project_budget_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.title} — {self.owner} ({self.status})"

    def is_client_editable(self):
        return self.status in CLIENT_EDITABLE_STATUSES

    def can_transition_to(self, new_status):
        if new_status == self.status:
            return True
        return new_status in ALLOWED_STATUS_TRANSITIONS.get(self.status, set())


class ProjectStatusChange(models.Model):
    """Append-only audit log — one row per status transition. Never
    updated or deleted; see services.change_project_status(), the only
    code path allowed to write project.status."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="status_changes")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="project_status_changes",
    )
    from_status = models.CharField(max_length=20)
    to_status = models.CharField(max_length=20)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project_id}: {self.from_status} → {self.to_status}"


class ProjectMessage(models.Model):
    """One row per chat message on a project thread. Same shape as
    contracts.Message — sender_type/sender_user/sender_name/body, plus
    independent read receipts for each side of the conversation."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="messages")

    sender_type = models.CharField(max_length=10, choices=MESSAGE_SENDER_TYPE_CHOICES, db_index=True)
    sender_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="project_messages",
    )
    sender_name = models.CharField(max_length=150, blank=True)

    body = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    read_by_admin_at = models.DateTimeField(null=True, blank=True)
    read_by_client_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["project", "sender_type", "read_by_admin_at"]),
            models.Index(fields=["project", "sender_type", "read_by_client_at"]),
        ]

    def __str__(self):
        return f"{self.project_id} [{self.sender_type}] {self.body[:40]}"
