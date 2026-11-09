from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import catalog, services
from .models import Order

SYNC = override_settings(ORDERS_SYNC_EMAIL=True, ORDER_NOTIFICATION_EMAILS=["admin@example.com"])


@SYNC
class OrderFlowTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters live in the cache; don't leak between tests
        self.c = APIClient()

    def _start(self, **extra):
        r = self.c.post("/api/orders/start/", {"service": "SPSS Data Analysis Help", "site": "thesis", **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["token"], r.json()["reference"]

    def test_catalog(self):
        r = self.c.get("/api/orders/catalog/?site=thesis")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["steps"][0]["key"], "service")
        self.assertIn("spss-data-analysis-help", [s["slug"] for s in body["services"]])

    def test_start_creates_draft_and_sends_nothing(self):
        token, ref = self._start()
        o = Order.objects.get(public_token=token)
        self.assertEqual(o.status, "draft")
        self.assertEqual(o.service_slug, "spss-data-analysis-help")
        self.assertTrue(ref.startswith("SCP-"))
        self.assertEqual(len(mail.outbox), 0)

    def test_service_label_from_old_landing_pages_still_resolves(self):
        token, _ = self._start()
        self.c.patch(f"/api/orders/{token}/", {"service": "Weird custom thing"}, format="json")
        o = Order.objects.get(public_token=token)
        self.assertEqual(o.service_slug, "other")
        self.assertEqual(o.service, "Other: Weird custom thing")

    def test_patch_saves_steps_and_merges_answers(self):
        token, _ = self._start()
        r = self.c.patch(f"/api/orders/{token}/", {
            "name": "W. Allan", "email": "A@Example.COM", "phone": "+254 718 889 559",
            "answers": {"heard_from": "google"}, "step": 2,
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.c.patch(f"/api/orders/{token}/", {"answers": {"sample_size": 120}}, format="json")
        o = Order.objects.get(public_token=token)
        self.assertEqual(o.email, "a@example.com")
        self.assertEqual(o.phone, "+254718889559")
        self.assertEqual(o.whatsapp_link, "https://wa.me/254718889559")
        self.assertEqual(o.extra_answers, {"heard_from": "google", "sample_size": 120})
        self.assertEqual(o.current_step, 2)

    def test_validation(self):
        token, _ = self._start()
        bad_phone = self.c.patch(f"/api/orders/{token}/", {"phone": "12"}, format="json")
        self.assertEqual(bad_phone.status_code, 400)
        past = self.c.patch(f"/api/orders/{token}/", {"deadline": "2001-01-01"}, format="json")
        self.assertEqual(past.status_code, 400)
        bad_key = self.c.patch(f"/api/orders/{token}/", {"answers": {"Bad Key!": "x"}}, format="json")
        self.assertEqual(bad_key.status_code, 400)

    def test_submit_requires_name_email_service(self):
        r = self.c.post("/api/orders/start/", {"site": "thesis"}, format="json")
        token = r.json()["token"]
        r = self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.json()), {"name", "email", "service"})
        self.assertEqual(len(mail.outbox), 0)

    def test_submit_notifies_admin_and_client_once(self):
        token, ref = self._start(name="W. Allan", email="w@example.com", phone="+254718889559",
                                 deadline=(timezone.localdate() + timedelta(days=2)).isoformat(),
                                 details="Need regression on 120 respondents.")
        r = self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(r.json()["already_submitted"])

        self.assertEqual(len(mail.outbox), 2)
        admin_mail = next(m for m in mail.outbox if m.to == ["admin@example.com"])
        client_mail = next(m for m in mail.outbox if m.to == ["w@example.com"])
        self.assertIn("[URGENT ORDER · thesis]", admin_mail.subject)
        self.assertIn(ref, admin_mail.subject)
        self.assertEqual(admin_mail.reply_to, ["w@example.com"])
        self.assertIn("wa.me/254718889559", admin_mail.alternatives[0][0])
        self.assertIn("regression on 120", admin_mail.body)
        self.assertIn(ref, client_mail.subject)

        o = Order.objects.get(public_token=token)
        self.assertEqual(o.status, "submitted")
        self.assertTrue(o.admin_notified and o.confirmation_sent)

        # double-click / retry: same reference, NO new emails
        r2 = self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertTrue(r2.json()["already_submitted"])
        self.assertEqual(len(mail.outbox), 2)

    def test_submit_body_can_carry_final_step(self):
        token, _ = self._start(name="Jo", email="jo@example.com")
        r = self.c.post(f"/api/orders/{token}/submit/", {"details": "last step text", "topic": "T"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Order.objects.get(public_token=token).details, "last step text")

    def test_cannot_edit_after_submit(self):
        token, _ = self._start(name="Jo", email="jo@example.com")
        self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertEqual(self.c.patch(f"/api/orders/{token}/", {"name": "Hacker"}, format="json").status_code, 409)
        up = SimpleUploadedFile("a.csv", b"x,y\n1,2\n")
        self.assertEqual(self.c.post(f"/api/orders/{token}/files/", {"file": up}, format="multipart").status_code, 409)

    def test_unknown_token_404(self):
        self.assertEqual(self.c.get("/api/orders/nope/").status_code, 404)

    def test_quick_order(self):
        r = self.c.post("/api/orders/quick/", {
            "site": "thesis", "name": "Quick Q", "email": "q@example.com", "service": "Regression Analysis Help",
            "details": "help", "page_url": "https://thesis.scapedatasolutions.com/regression-analysis-help",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(len(mail.outbox), 2)
        o = Order.objects.get()
        self.assertEqual(o.status, "submitted")
        self.assertIn("regression-analysis-help", o.source_url)
        bad = self.c.post("/api/orders/quick/", {"name": "x"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)  # invalid quick order leaves no stray draft

    def test_honeypot_stores_nothing(self):
        r = self.c.post("/api/orders/quick/", {"name": "Bot", "email": "b@example.com",
                                               "service": "x", "website": "http://spam"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_header_injection_in_name_is_harmless(self):
        token, _ = self._start(name="Evil\r\nBcc: x@y.com", email="e@example.com")
        r = self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        for m in mail.outbox:
            self.assertNotIn("\n", m.subject)

    def test_file_upload_and_delete(self):
        token, _ = self._start()
        with mock.patch("orders.views.upload_order_file", return_value=("https://s/x.csv", "orders/p/x.csv")), \
             mock.patch("orders.views.delete_order_file") as deleter:
            up = SimpleUploadedFile("data.csv", b"a,b\n1,2\n", content_type="text/csv")
            r = self.c.post(f"/api/orders/{token}/files/", {"file": up}, format="multipart")
            self.assertEqual(r.status_code, 201, r.content)
            fid = r.json()["id"]
            exe = SimpleUploadedFile("virus.exe", b"MZ")
            self.assertEqual(self.c.post(f"/api/orders/{token}/files/", {"file": exe}, format="multipart").status_code, 400)
            self.assertEqual(self.c.delete(f"/api/orders/{token}/files/{fid}/").status_code, 204)
            deleter.assert_called_once_with("orders/p/x.csv")
        self.assertEqual(Order.objects.get(public_token=token).files.count(), 0)

    def test_file_listed_in_admin_email(self):
        token, _ = self._start(name="Jo", email="jo@example.com")
        o = Order.objects.get(public_token=token)
        o.files.create(original_name="survey.sav", url="https://s/survey.sav", size_bytes=10)
        self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        admin_mail = next(m for m in mail.outbox if m.to == ["admin@example.com"])
        self.assertIn("survey.sav", admin_mail.body)


@SYNC
class SweepTests(TestCase):
    def _draft(self, minutes_old, **kw):
        o = Order.objects.create(service="SPSS Data Analysis Help", service_slug="spss-data-analysis-help", **kw)
        Order.objects.filter(pk=o.pk).update(last_activity_at=timezone.now() - timedelta(minutes=minutes_old))
        return o

    def test_abandoned_draft_alerts_admin_and_client_once(self):
        o = self._draft(45, name="Ann", email="ann@example.com", phone="+254700000001")
        stats = services.sweep_abandoned_orders()
        self.assertEqual((stats["abandoned"], stats["recovery"]), (1, 1))
        subjects = [m.subject for m in mail.outbox]
        self.assertTrue(any("[ABANDONED ORDER" in s for s in subjects))
        client = next(m for m in mail.outbox if m.to == ["ann@example.com"])
        self.assertIn(f"/order-now?resume={o.public_token}", client.body)
        services.sweep_abandoned_orders()
        self.assertEqual(len(mail.outbox), 2)  # second sweep is silent

    def test_fresh_draft_and_contactless_draft_ignored(self):
        self._draft(5, email="fresh@example.com")
        self._draft(90)  # old but we have no way to reach them
        self.assertEqual(sum(services.sweep_abandoned_orders().values()), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_converted_person_not_nagged(self):
        self._draft(60, name="Bo", email="bo@example.com")
        done = Order.objects.create(name="Bo", email="bo@example.com", service="x", status="submitted",
                                    submitted_at=timezone.now())
        self.assertEqual(done.status, "submitted")
        stats = services.sweep_abandoned_orders()
        self.assertEqual(stats["converted"], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_stuck_submission_is_retried(self):
        o = Order.objects.create(name="Cy", email="cy@example.com", service="x", status="submitted",
                                 submitted_at=timezone.now() - timedelta(minutes=10), admin_notified=False)
        stats = services.sweep_abandoned_orders()
        self.assertEqual(stats["retried"], 1)
        self.assertTrue(Order.objects.get(pk=o.pk).admin_notified)


THESIS_ORIGIN = "https://thesis.scapedatasolutions.com"
MAIN_ORIGIN = "https://www.scapedatasolutions.com"


@SYNC
class MultiSiteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def test_catalog_is_per_site_via_origin(self):
        thesis = self.c.get("/api/orders/catalog/", HTTP_ORIGIN=THESIS_ORIGIN).json()
        main = self.c.get("/api/orders/catalog/", HTTP_ORIGIN=MAIN_ORIGIN).json()
        self.assertEqual((thesis["site"]["key"], main["site"]["key"]), ("thesis", "main"))
        self.assertIn("spss-data-analysis-help", [s["slug"] for s in thesis["services"]])
        self.assertNotIn("spss-data-analysis-help", [s["slug"] for s in main["services"]])
        self.assertIn("budget", main["options"])
        self.assertNotIn("budget", thesis["options"])

    def test_order_gets_site_from_origin_and_explicit_beats_origin(self):
        r = self.c.post("/api/orders/start/", {}, format="json", HTTP_ORIGIN=THESIS_ORIGIN)
        self.assertEqual(r.json()["order"]["site"], "thesis")
        r = self.c.post("/api/orders/start/", {}, format="json", HTTP_ORIGIN=MAIN_ORIGIN)
        self.assertEqual(r.json()["order"]["site"], "main")
        r = self.c.post("/api/orders/start/", {"site": "thesis"}, format="json", HTTP_ORIGIN=MAIN_ORIGIN)
        self.assertEqual(r.json()["order"]["site"], "thesis")
        r = self.c.post("/api/orders/start/", {}, format="json", HTTP_ORIGIN="http://localhost:5173")
        self.assertEqual(r.json()["order"]["site"], catalog.DEFAULT_SITE)

    def test_custom_fields_go_to_answers_with_labels(self):
        r = self.c.post("/api/orders/start/", {
            "site": "main", "service": "Dashboards and Reporting", "name": "Dee", "email": "dee@example.com",
            "budget": "$500 - $2,000", "company": "Acme Ltd", "not_declared": "ignored",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        o = Order.objects.get()
        self.assertEqual(o.extra_answers, {"budget": "$500 - $2,000", "company": "Acme Ltd"})
        self.c.patch(f"/api/orders/{o.public_token}/", {"company_size": "11-50"}, format="json")
        self.c.post(f"/api/orders/{o.public_token}/submit/", {}, format="json")
        admin_mail = next(m for m in mail.outbox if m.to == ["admin@example.com"])
        self.assertIn("Budget range: $500 - $2,000", admin_mail.body)
        self.assertIn("Company size: 11-50", admin_mail.body)
        self.assertIn("Scape Data Solutions", admin_mail.body.split("\n")[0] + admin_mail.body)
        self.assertIn("[NEW ORDER · main]", admin_mail.subject)

    def test_custom_field_can_satisfy_a_site_requirement(self):
        site = catalog.build_site(
            "shop", name="Shop", hosts=["shop.example.org"], frontend_url="https://shop.example.org",
            services=[], allow_other=False, required=("name", "email", "budget"),
            steps=[{"key": "b", "title": "Budget", "input": "cards", "fields": ["budget"], "required": []}],
        )
        with mock.patch.dict(catalog.SITES, {"shop": site}):
            bad = self.c.post("/api/orders/quick/", {"site": "shop", "name": "Al", "email": "a@example.com"}, format="json")
            self.assertEqual(set(bad.json()), {"budget"})
            ok = self.c.post("/api/orders/quick/", {"site": "shop", "name": "Al", "email": "a@example.com",
                                                    "budget": "5k"}, format="json")
            self.assertEqual(ok.status_code, 201, ok.content)

    def test_per_site_notification_override(self):
        with mock.patch.dict(catalog.THESIS, {"notify_emails": ["thesis-desk@example.com"]}):
            self.c.post("/api/orders/quick/", {"site": "thesis", "name": "Tee", "email": "t@example.com",
                                               "service": "x"}, format="json")
        self.assertTrue(any(m.to == ["thesis-desk@example.com"] for m in mail.outbox))
        self.assertFalse(any(m.to == ["admin@example.com"] for m in mail.outbox))

    def test_resume_link_points_at_the_orders_own_site(self):
        for site, base in (("thesis", "https://thesis.scapedatasolutions.com"), ("main", "https://www.scapedatasolutions.com")):
            o = Order.objects.create(site=site, name="R", email=f"{site}@example.com")
            Order.objects.filter(pk=o.pk).update(last_activity_at=timezone.now() - timedelta(minutes=60))
            mail.outbox.clear()
            services.sweep_abandoned_orders()
            client = next(m for m in mail.outbox if m.to == [f"{site}@example.com"])
            self.assertIn(f"{base}/order-now?resume={o.public_token}", client.body)

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=[], CORS_ALLOWED_ORIGIN_REGEXES=[])
    def test_cors_opens_automatically_for_a_site_on_a_new_domain(self):
        site = catalog.build_site("shop", name="Shop", hosts=["orders.brandnew.org"],
                                  frontend_url="https://orders.brandnew.org", services=[], steps=[])
        def preflight(origin, path="/api/orders/start/"):
            return self.c.options(path, HTTP_ORIGIN=origin, HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST")
        self.assertIsNone(preflight("https://orders.brandnew.org").headers.get("Access-Control-Allow-Origin"))
        with mock.patch.dict(catalog.SITES, {"shop": site}):
            self.assertEqual(preflight("https://orders.brandnew.org").headers.get("Access-Control-Allow-Origin"),
                             "https://orders.brandnew.org")
            # ...but only for the orders API, and never for unregistered origins
            self.assertIsNone(preflight("https://orders.brandnew.org", "/api/other/").headers.get("Access-Control-Allow-Origin"))
            self.assertIsNone(preflight("https://evil.example").headers.get("Access-Control-Allow-Origin"))
