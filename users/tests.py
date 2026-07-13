from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .models import UserSignup


def make_signup(**overrides):
    defaults = dict(
        full_name="Jane Wanjiru",
        email="jane@example.com",
        phone="+254712345678",
        consent_given=True,
        consent_given_at=timezone.now(),
    )
    defaults.update(overrides)
    return UserSignup.objects.create(**defaults)


class UserSignupModelTests(TestCase):
    def test_generate_and_verify_token_roundtrip(self):
        signup = make_signup()
        raw_token = signup.generate_verification_token()
        self.assertTrue(signup.verify_token(raw_token))

    def test_verify_token_rejects_wrong_token(self):
        signup = make_signup()
        signup.generate_verification_token()
        self.assertFalse(signup.verify_token("not-the-token"))

    def test_verify_token_rejects_expired_token(self):
        signup = make_signup()
        raw_token = signup.generate_verification_token()
        signup.email_verification_expires_at = timezone.now() - timezone.timedelta(hours=1)
        signup.save(update_fields=["email_verification_expires_at"])
        self.assertFalse(signup.verify_token(raw_token))

    def test_mark_verified_spends_the_token(self):
        signup = make_signup()
        raw_token = signup.generate_verification_token()
        signup.mark_verified()
        self.assertTrue(signup.email_verified)
        self.assertEqual(signup.email_verification_token_hash, "")
        # Already-verified signups short-circuit to True regardless of token.
        self.assertTrue(signup.verify_token(raw_token))

    def test_new_token_invalidates_old_one(self):
        signup = make_signup()
        old_token = signup.generate_verification_token()
        new_token = signup.generate_verification_token()
        self.assertFalse(signup.verify_token(old_token))
        self.assertTrue(signup.verify_token(new_token))


class SignupViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @patch("users.views.send_verification_email")
    def test_signup_creates_record_and_sends_email(self, mock_send):
        mock_send.return_value = True
        response = self.client.post(
            reverse("users:signup"),
            {
                "full_name": "Jane Wanjiru",
                "email": "jane@example.com",
                "phone": "+254712345678",
                "consent_given": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(UserSignup.objects.filter(email="jane@example.com").exists())
        mock_send.assert_called_once()

    def test_signup_rejects_missing_consent(self):
        response = self.client.post(
            reverse("users:signup"),
            {"full_name": "Jane Wanjiru", "email": "jane@example.com", "phone": "+254712345678", "consent_given": False},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_signup_rejects_duplicate_email(self):
        make_signup()
        response = self.client.post(
            reverse("users:signup"),
            {"full_name": "Another Name", "email": "jane@example.com", "phone": "+254700000000", "consent_given": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_signup_rejects_invalid_phone(self):
        response = self.client.post(
            reverse("users:signup"),
            {"full_name": "Jane Wanjiru", "email": "jane2@example.com", "phone": "not-a-phone", "consent_given": True},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class VerifyEmailViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_confirm_with_valid_token_marks_verified(self):
        signup = make_signup()
        raw_token = signup.generate_verification_token()
        response = self.client.post(
            reverse("users:verify-email-confirm"),
            {"id": str(signup.id), "token": raw_token},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        signup.refresh_from_db()
        self.assertTrue(signup.email_verified)

    def test_confirm_with_bad_token_returns_400(self):
        signup = make_signup()
        signup.generate_verification_token()
        response = self.client.post(
            reverse("users:verify-email-confirm"),
            {"id": str(signup.id), "token": "wrong"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    @patch("users.views.send_verification_email")
    def test_resend_is_silent_for_unknown_email(self, mock_send):
        response = self.client.post(
            reverse("users:verify-email-resend"),
            {"email": "nobody@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        mock_send.assert_not_called()

    @patch("users.views.send_verification_email")
    def test_resend_issues_new_token_for_unverified_signup(self, mock_send):
        mock_send.return_value = True
        signup = make_signup()
        old_token = signup.generate_verification_token()
        response = self.client.post(
            reverse("users:verify-email-resend"),
            {"email": signup.email},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        signup.refresh_from_db()
        self.assertFalse(signup.verify_token(old_token))
        mock_send.assert_called_once()

    @patch("users.views.send_verification_email")
    def test_resend_noop_for_already_verified_signup(self, mock_send):
        signup = make_signup()
        signup.generate_verification_token()
        signup.mark_verified()
        response = self.client.post(
            reverse("users:verify-email-resend"),
            {"email": signup.email},
            format="json",
        )
        self.assertEqual(response.status_code, 202)
        mock_send.assert_not_called()
