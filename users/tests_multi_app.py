from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from users.app_registry import detect_source, resolve_source
from users.models import AppMembership, UserSignup

APPS = {
    "property": {"name": "Scape Property Intelligence", "base_url": "https://app.example.com", "show_free_reports": True},
    "portal": {"name": "Scape Client Portal", "base_url": "https://portal.example.com",
               "extra_origins": ["https://www.portal.example.com"]},
    "thesis": {"name": "Scape Thesis", "base_url": "https://thesis.example.com"},
}
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD = "Str0ng-Passw0rd!x"


@override_settings(
    SCAPE_APPS=APPS, SCAPE_DEFAULT_APP="property", SCAPE_RESEND_COOLDOWN_SECONDS=60,
    CACHES=LOCMEM_CACHE, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class MultiAppTests(TestCase):
    def setUp(self):
        cache.clear()
        patcher = patch("visitors.middleware._trigger_enrichment")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()

    def signup(self, email="ada@example.com", origin=None, **extra):
        body = {"full_name": "Ada Lovelace", "email": email, "country": "US",
                "consent_given": True, "password": PASSWORD}
        body.update(extra)
        headers = {"HTTP_ORIGIN": origin} if origin else {}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/users/signup/", body, format="json", secure=True, **headers)

    def fresh_token(self, email):
        return UserSignup.objects.get(email=email).generate_verification_token()

    # ---- registry ----
    def test_detect_and_resolve(self):
        rf = RequestFactory()
        self.assertEqual(detect_source(explicit="portal"), "portal")
        self.assertIsNone(detect_source(explicit="https://evil.example"))
        self.assertEqual(detect_source(rf.get("/", HTTP_ORIGIN="https://portal.example.com")), "portal")
        self.assertEqual(detect_source(rf.get("/", HTTP_ORIGIN="https://www.portal.example.com")), "portal")
        self.assertEqual(detect_source(rf.get("/", HTTP_REFERER="https://thesis.example.com/signup?x=1")), "thesis")
        self.assertIsNone(detect_source(rf.get("/", HTTP_ORIGIN="null")))
        self.assertEqual(resolve_source(rf.get("/"), None, fallback="thesis"), "thesis")
        self.assertEqual(resolve_source(rf.get("/")), "property")

    # ---- signup routing ----
    def test_portal_signup_gets_portal_link_and_branding(self):
        r = self.signup(source="portal")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["signup_source"], "portal")
        msg = mail.outbox[-1]
        self.assertIn("https://portal.example.com/verify-email?token=", msg.body)
        self.assertNotIn("app.example.com", msg.body)
        self.assertEqual(msg.subject, "Verify your Scape Client Portal account")

    def test_source_inferred_from_origin(self):
        self.signup(email="o@example.com", origin="https://www.portal.example.com")
        self.assertEqual(UserSignup.objects.get(email="o@example.com").signup_source, "portal")

    def test_unknown_source_is_ignored_not_an_error(self):
        r = self.signup(email="u@example.com", source="https://evil.example/phish")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["signup_source"], "property")
        self.assertIn("https://app.example.com/verify-email", mail.outbox[-1].body)

    def test_no_signal_defaults_to_property(self):
        self.signup(email="d@example.com")
        self.assertEqual(UserSignup.objects.get(email="d@example.com").signup_source, "property")

    # ---- verification ----
    def test_verify_is_idempotent_and_welcome_sent_once(self):
        self.signup(source="portal")
        raw = self.fresh_token("ada@example.com")
        signup = UserSignup.objects.get(email="ada@example.com")
        before = len(mail.outbox)
        for _ in range(3):
            with self.captureOnCommitCallbacks(execute=True):
                r = self.client.post("/api/users/verify-email/confirm/",
                                     {"id": str(signup.id), "token": raw}, format="json", secure=True)
            self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(len(mail.outbox) - before, 1)
        welcome = mail.outbox[-1]
        self.assertEqual(welcome.subject, "Welcome to Scape Client Portal")
        self.assertNotIn("free property reports", welcome.body)
        self.assertEqual(r.json()["login_url"], "https://portal.example.com/login")

    def test_bad_token_and_unknown_id_are_generic_400(self):
        self.signup()
        signup = UserSignup.objects.get(email="ada@example.com")
        r1 = self.client.post("/api/users/verify-email/confirm/",
                              {"id": str(signup.id), "token": "nope"}, format="json", secure=True)
        r2 = self.client.post("/api/users/verify-email/confirm/",
                              {"id": "11111111-1111-1111-1111-111111111111", "token": "nope"},
                              format="json", secure=True)
        self.assertEqual((r1.status_code, r2.status_code), (400, 400))
        self.assertEqual(r1.json(), r2.json())

    # ---- resend ----
    def test_resend_cooldown_then_goes_to_current_app(self):
        self.signup(source="property")
        sent = len(mail.outbox)
        r = self.client.post("/api/users/verify-email/resend/", {"email": "ada@example.com"}, format="json", secure=True)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(mail.outbox), sent)  # throttled
        cache.clear()
        r = self.client.post("/api/users/verify-email/resend/",
                             {"email": "ada@example.com", "source": "thesis"}, format="json", secure=True)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(mail.outbox), sent + 1)
        self.assertIn("https://thesis.example.com/verify-email", mail.outbox[-1].body)

    def test_resend_unknown_email_is_202_and_silent(self):
        r = self.client.post("/api/users/verify-email/resend/", {"email": "ghost@example.com"}, format="json", secure=True)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(len(mail.outbox), 0)

    # ---- one account, many apps ----
    def test_duplicate_signup_returns_account_exists_code(self):
        self.signup(source="property")
        r = self.signup(source="portal")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "account_exists")
        self.assertIn("email", r.json())

    def test_login_from_other_app_records_membership(self):
        self.signup(source="property")
        r = self.client.post("/api/users/login/",
                             {"email": "ada@example.com", "password": PASSWORD, "source": "portal"},
                             format="json", secure=True)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(set(r.json()["user"]["apps"]), {"property", "portal"})
        self.assertEqual(AppMembership.objects.filter(user__username="ada@example.com").count(), 2)

    def test_login_without_app_signal_records_nothing_extra(self):
        self.signup(source="property")
        self.client.post("/api/users/login/", {"email": "ada@example.com", "password": PASSWORD}, format="json", secure=True)
        self.assertEqual(AppMembership.objects.filter(user__username="ada@example.com").count(), 1)

    # ---- password reset ----
    def test_password_reset_link_goes_to_requesting_app(self):
        self.signup(source="property")
        cache.clear()
        r = self.client.post("/api/users/password-reset/request/",
                             {"email": "ada@example.com", "source": "portal"}, format="json", secure=True)
        self.assertEqual(r.status_code, 202)
        self.assertIn("https://portal.example.com/reset-password?uid=", mail.outbox[-1].body)

    # ---- atomicity ----
    def test_failed_profile_insert_leaves_no_orphan_auth_user(self):
        from django.contrib.auth import get_user_model
        with patch("users.serializers.record_app_membership", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.raise_request_exception = True
                self.signup(email="orphan@example.com")
        self.assertFalse(get_user_model().objects.filter(username="orphan@example.com").exists())
        self.assertFalse(UserSignup.objects.filter(email="orphan@example.com").exists())
