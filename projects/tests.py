from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Project, ProjectMessage
from .services import ProjectConversionError, convert_project_to_contract


def make_user(**overrides):
    defaults = dict(username="client@example.com", email="client@example.com", password="testpass123")
    defaults.update(overrides)
    User = get_user_model()
    return User.objects.create_user(**defaults)


def make_staff(**overrides):
    defaults = dict(username="admin@example.com", email="admin@example.com", password="testpass123", is_staff=True)
    defaults.update(overrides)
    User = get_user_model()
    return User.objects.create_user(**defaults)


def auth_client(user):
    client = APIClient()
    access = str(RefreshToken.for_user(user).access_token)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client


def make_project(owner, **overrides):
    defaults = dict(title="Landing page", description="Need a one-page marketing site with a signup form.")
    defaults.update(overrides)
    return Project.objects.create(owner=owner, **defaults)


class ProjectModelTests(TestCase):
    def setUp(self):
        self.owner = make_user()

    def test_is_client_editable_true_for_submitted(self):
        project = make_project(self.owner, status="submitted")
        self.assertTrue(project.is_client_editable())

    def test_is_client_editable_false_once_in_progress(self):
        project = make_project(self.owner, status="in_progress")
        self.assertFalse(project.is_client_editable())

    def test_can_transition_to_allows_defined_paths(self):
        project = make_project(self.owner, status="submitted")
        self.assertTrue(project.can_transition_to("under_review"))
        self.assertTrue(project.can_transition_to("cancelled"))

    def test_can_transition_to_rejects_undefined_paths(self):
        project = make_project(self.owner, status="submitted")
        self.assertFalse(project.can_transition_to("completed"))

    def test_terminal_statuses_have_no_transitions_out(self):
        project = make_project(self.owner, status="completed")
        self.assertFalse(project.can_transition_to("in_progress"))
        self.assertFalse(project.can_transition_to("submitted"))


class ProjectListCreateViewTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.other = make_user(username="other@example.com", email="other@example.com")
        self.client_api = auth_client(self.owner)

    def test_unauthenticated_request_rejected(self):
        response = APIClient().get(reverse("projects-list-create"))
        self.assertEqual(response.status_code, 401)

    def test_create_project(self):
        response = self.client_api.post(
            reverse("projects-list-create"),
            {"title": "Data scraping job", "description": "Scrape 500 product listings from a public catalog site."},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(Project.objects.filter(title="Data scraping job", owner=self.owner).exists())

    def test_create_project_rejects_short_description(self):
        response = self.client_api.post(
            reverse("projects-list-create"),
            {"title": "Short", "description": "too short"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_list_only_returns_own_projects(self):
        make_project(self.owner, title="Mine")
        make_project(self.other, title="Not mine")
        response = self.client_api.get(reverse("projects-list-create"))
        self.assertEqual(response.status_code, 200)
        titles = [p["title"] for p in response.data["results"]] if "results" in response.data else [p["title"] for p in response.data]
        self.assertIn("Mine", titles)
        self.assertNotIn("Not mine", titles)


class ProjectDetailViewTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.other = make_user(username="other@example.com", email="other@example.com")
        self.project = make_project(self.owner)

    def test_owner_can_view(self):
        client_api = auth_client(self.owner)
        response = client_api.get(reverse("projects-detail", args=[self.project.id]))
        self.assertEqual(response.status_code, 200)

    def test_non_owner_forbidden(self):
        client_api = auth_client(self.other)
        response = client_api.get(reverse("projects-detail", args=[self.project.id]))
        self.assertEqual(response.status_code, 403)

    def test_owner_can_edit_while_submitted(self):
        client_api = auth_client(self.owner)
        response = client_api.patch(
            reverse("projects-detail", args=[self.project.id]), {"title": "Updated title"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.title, "Updated title")

    def test_owner_cannot_edit_once_in_progress(self):
        self.project.status = "in_progress"
        self.project.save(update_fields=["status"])
        client_api = auth_client(self.owner)
        response = client_api.patch(
            reverse("projects-detail", args=[self.project.id]), {"title": "Nope"}, format="json",
        )
        self.assertEqual(response.status_code, 400)


class ProjectMessagingTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.staff = make_staff()
        self.project = make_project(self.owner)

    def test_client_can_send_and_read_own_thread(self):
        client_api = auth_client(self.owner)
        response = client_api.post(
            reverse("projects-messages", args=[self.project.id]), {"body": "Hi, any questions?"}, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ProjectMessage.objects.filter(project=self.project, sender_type="client").count(), 1)

    def test_admin_can_reply(self):
        client_api = auth_client(self.staff)
        response = client_api.post(
            reverse("projects-admin-messages", args=[self.project.id]), {"body": "Sure, what's your timeline?"}, format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ProjectMessage.objects.filter(project=self.project, sender_type="admin").count(), 1)

    def test_empty_body_rejected(self):
        client_api = auth_client(self.owner)
        response = client_api.post(
            reverse("projects-messages", args=[self.project.id]), {"body": "   "}, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_mark_read_updates_admin_side(self):
        ProjectMessage.objects.create(project=self.project, sender_type="admin", sender_name="Admin", body="hello")
        client_api = auth_client(self.owner)
        response = client_api.post(reverse("projects-messages-mark-read", args=[self.project.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["marked_read"], 1)


class AdminProjectViewTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.staff = make_staff()
        self.project = make_project(self.owner)

    def test_non_staff_forbidden_from_admin_list(self):
        client_api = auth_client(self.owner)
        response = client_api.get(reverse("projects-admin-list"))
        self.assertEqual(response.status_code, 403)

    def test_staff_can_list_all_projects(self):
        client_api = auth_client(self.staff)
        response = client_api.get(reverse("projects-admin-list"))
        self.assertEqual(response.status_code, 200)

    def test_valid_status_transition_succeeds(self):
        client_api = auth_client(self.staff)
        response = client_api.patch(
            reverse("projects-admin-detail", args=[self.project.id]), {"status": "under_review"}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "under_review")

    def test_invalid_status_transition_rejected(self):
        client_api = auth_client(self.staff)
        response = client_api.patch(
            reverse("projects-admin-detail", args=[self.project.id]), {"status": "completed"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "submitted")


class ConvertToContractTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.staff = make_staff()
        self.project = make_project(self.owner, budget=25000)

    def test_convert_creates_contract_and_links_it(self):
        contract = convert_project_to_contract(self.project, created_by=self.staff)
        self.project.refresh_from_db()
        self.assertEqual(self.project.contract_id, contract.id)
        self.assertEqual(self.project.status, "in_progress")
        self.assertEqual(contract.client_email, self.owner.email)
        self.assertEqual(contract.total_value, self.project.budget)

    def test_cannot_convert_twice(self):
        convert_project_to_contract(self.project, created_by=self.staff)
        with self.assertRaises(ProjectConversionError):
            convert_project_to_contract(self.project, created_by=self.staff)

    def test_convert_endpoint_staff_only(self):
        client_api = auth_client(self.owner)
        response = client_api.post(reverse("projects-admin-convert", args=[self.project.id]))
        self.assertEqual(response.status_code, 403)

    def test_convert_endpoint_success(self):
        client_api = auth_client(self.staff)
        response = client_api.post(reverse("projects-admin-convert", args=[self.project.id]), {}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("contract_id", response.data)


class AttachmentUploadTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.client_api = auth_client(self.owner)

    def test_rejects_disallowed_extension(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        bad_file = SimpleUploadedFile("virus.exe", b"not really a virus", content_type="application/octet-stream")
        response = self.client_api.post(
            reverse("projects-list-create"),
            {"title": "Some project", "description": "A perfectly reasonable description of the work.", "attachment": bad_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_successful_upload_sets_attachment_fields(self):
        from unittest.mock import patch as mock_patch
        from django.core.files.uploadedfile import SimpleUploadedFile

        good_file = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 fake pdf bytes", content_type="application/pdf")
        with mock_patch(
            "projects.storage.upload_project_attachment",
            return_value=("https://fake.supabase.co/storage/v1/object/public/project-attachments/x/brief.pdf", "brief.pdf"),
        ):
            response = self.client_api.post(
                reverse("projects-list-create"),
                {"title": "Some project", "description": "A perfectly reasonable description of the work.", "attachment": good_file},
                format="multipart",
            )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["attachment_url"].startswith("https://"))
        self.assertEqual(response.data["attachment_filename"], "brief.pdf")

    def test_upload_failure_returns_502_not_a_silently_missing_attachment(self):
        from unittest.mock import patch as mock_patch
        from django.core.files.uploadedfile import SimpleUploadedFile
        from . import storage

        good_file = SimpleUploadedFile("brief.pdf", b"%PDF-1.4 fake pdf bytes", content_type="application/pdf")
        with mock_patch(
            "projects.storage.upload_project_attachment",
            side_effect=storage.StorageUploadFailed("bucket not found"),
        ):
            response = self.client_api.post(
                reverse("projects-list-create"),
                {"title": "Some project", "description": "A perfectly reasonable description of the work.", "attachment": good_file},
                format="multipart",
            )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(Project.objects.filter(title="Some project").exists())


class StatusAuditTrailTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.staff = make_staff()
        self.project = make_project(self.owner)

    def test_status_change_writes_audit_row(self):
        client_api = auth_client(self.staff)
        response = client_api.patch(
            reverse("projects-admin-detail", args=[self.project.id]),
            {"status": "under_review", "note": "Looks doable, scoping now."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        history = client_api.get(reverse("projects-admin-status-history", args=[self.project.id]))
        self.assertEqual(history.status_code, 200)
        self.assertEqual(len(history.data), 1)
        self.assertEqual(history.data[0]["from_status"], "submitted")
        self.assertEqual(history.data[0]["to_status"], "under_review")
        self.assertEqual(history.data[0]["note"], "Looks doable, scoping now.")

    def test_invalid_transition_does_not_write_audit_row(self):
        client_api = auth_client(self.staff)
        client_api.patch(reverse("projects-admin-detail", args=[self.project.id]), {"status": "completed"}, format="json")
        history = client_api.get(reverse("projects-admin-status-history", args=[self.project.id]))
        self.assertEqual(len(history.data), 0)


class UnreadSummaryTests(TestCase):
    def setUp(self):
        self.owner = make_user()
        self.staff = make_staff()
        self.project = make_project(self.owner)

    def test_client_unread_count(self):
        ProjectMessage.objects.create(project=self.project, sender_type="admin", sender_name="Admin", body="hi")
        client_api = auth_client(self.owner)
        response = client_api.get(reverse("projects-unread-count"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread_messages"], 1)

    def test_admin_unread_count_and_pending_review(self):
        ProjectMessage.objects.create(project=self.project, sender_type="client", sender_name="Client", body="hi")
        client_api = auth_client(self.staff)
        response = client_api.get(reverse("projects-admin-unread-count"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["unread_messages"], 1)
        self.assertEqual(response.data["pending_review"], 1)
