"""
projects/services.py

Orchestration layer, same role as contracts/services.py: views stay
thin, this is where the actual logic + side effects (attachment
upload, email, audit logging) live.
"""
import logging

from . import emails, storage
from .models import Project, ProjectMessage, ProjectStatusChange

logger = logging.getLogger("projects")


class ProjectConversionError(Exception):
    """Raised when a project can't be converted into a Contract."""


class AttachmentUploadError(Exception):
    """Raised when Supabase upload fails — surfaced as a 502, not
    silently dropped, so a client never believes a file attached when
    it didn't (see views.py)."""


def _attach_file(project, uploaded_file):
    """Uploads to Supabase and sets the three attachment_* fields on
    the (unsaved-yet) project instance. Caller is responsible for
    saving. Raises AttachmentUploadError on failure — deliberately NOT
    swallowed like the email side effects below, because a missing
    attachment is a data-loss bug from the client's point of view, not
    a "nice to have" notification."""
    try:
        url, original_name = storage.upload_project_attachment(uploaded_file, project.id)
    except storage.StorageUploadFailed as exc:
        logger.exception("Attachment upload failed for project %s", project.id)
        raise AttachmentUploadError(str(exc)) from exc

    project.attachment_url = url
    project.attachment_filename = original_name
    project.attachment_content_type = uploaded_file.content_type or ""


def create_project(*, owner, validated_data):
    """THE single place a Project gets created from client input —
    called by ProjectListCreateView."""
    attachment = validated_data.pop("attachment", None)

    project = Project(owner=owner, **validated_data)
    if attachment is not None:
        _attach_file(project, attachment)  # raises before save() if upload fails
    project.save()

    try:
        emails.send_project_submitted_email(project)
    except Exception:
        logger.exception("Failed to send project-submitted email for project %s", project.id)
    return project


def update_project(*, project, validated_data):
    """THE single place an existing Project gets edited by its owner —
    called by ProjectDetailView.patch. Handles the optional attachment
    replacement the same way create_project does."""
    attachment = validated_data.pop("attachment", None)

    for field, value in validated_data.items():
        setattr(project, field, value)
    if attachment is not None:
        _attach_file(project, attachment)
    project.save()
    return project


def post_project_message(*, project, sender_type, sender_user=None, sender_name="", body):
    """
    THE single place a ProjectMessage gets created — called by
    ClientMessageListCreateView, AdminMessageListCreateView, AND
    ProjectMessageConsumer (the websocket path). Keeping creation in
    one place means the real-time broadcast
    (signals.broadcast_new_project_message, a post_save receiver) and
    the email notification below fire for every message no matter
    which path sent it — including one created directly in Django
    admin, for free.
    """
    message = ProjectMessage.objects.create(
        project=project, sender_type=sender_type, sender_user=sender_user,
        sender_name=sender_name, body=body,
    )
    try:
        emails.send_new_message_notification(message)
    except Exception:
        logger.exception("Failed to send new-message notification for message %s", message.id)
    return message


def change_project_status(*, project, new_status, changed_by, note=""):
    """
    THE single place project.status is ever allowed to change. Writes
    a ProjectStatusChange audit row every time — this is what makes
    "who moved this to cancelled and when" answerable later, rather
    than only visible as an overwritten updated_at timestamp. Called by
    AdminProjectDetailView.patch AND convert_project_to_contract below,
    so a conversion's implicit status jump to "in_progress" is logged
    exactly like a manual one.
    """
    if new_status == project.status:
        return project

    if not project.can_transition_to(new_status):
        raise ProjectConversionError(f"Can't move a project from '{project.status}' to '{new_status}'.")

    old_status = project.status
    project.status = new_status
    project.save(update_fields=["status", "updated_at"])

    ProjectStatusChange.objects.create(
        project=project, changed_by=changed_by, from_status=old_status, to_status=new_status, note=note,
    )
    logger.info("Project %s: %s -> %s (by %s)", project.id, old_status, new_status, changed_by)
    return project


def convert_project_to_contract(project, *, created_by, title=None, scope_of_work=None,
                                 total_value=None, currency=None):
    """
    The bridge into contracts/milestones/payments. Creates a real
    contracts.Contract seeded from this project (client identity +
    brief), links it back via project.contract, and moves the project
    to "in_progress" via change_project_status() (so this shows up in
    the audit trail too) — everything downstream of this call
    (negotiation, e-signature, Milestone creation, Paystack
    invoicing/payment) is handled entirely by the existing contracts
    app; this function does not touch any of that logic, it only
    creates the Contract row and hands off.

    Idempotent by rejection, not by silently returning the existing
    link: calling this twice on an already-converted project raises,
    so a double-click in the admin can't create two contracts for one
    project.
    """
    from contracts.models import Contract  # local import — see module docstring in models.py

    if project.contract_id:
        raise ProjectConversionError("This project has already been converted to a contract.")

    contract = Contract.objects.create(
        created_by=created_by,
        client_name=project.owner.get_full_name() or project.owner.get_username(),
        client_email=project.owner.email,
        title=title or project.title,
        scope_of_work=scope_of_work if scope_of_work is not None else project.description,
        total_value=total_value if total_value is not None else project.budget,
        currency=currency or project.currency,
        client_user=project.owner,
    )

    project.contract = contract
    project.save(update_fields=["contract", "updated_at"])

    if project.status != "in_progress" and project.can_transition_to("in_progress"):
        change_project_status(
            project=project, new_status="in_progress", changed_by=created_by,
            note=f"Converted to contract {contract.id}.",
        )

    logger.info("Project %s converted to contract %s", project.id, contract.id)
    return contract
