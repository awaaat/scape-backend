from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import engagement_engine as engine
from . import engagement_hooks, engagement_intel as intel, engagement_notifications as notify, services
from .models import EmailSuppression, Order, OrderEngagement, OrderReminderLog

ADMINS = ["a@example.com", "b@example.com", "c@example.com"]
ENG = override_settings(
    ORDER_ENGAGEMENT_ENABLED=True, ORDERS_SYNC_EMAIL=True, ORDER_NOTIFICATION_EMAILS=ADMINS,
    ORDERS_API_BASE_URL="https://api.example.com", ORDER_ENGAGEMENT_SINCE="",
    ORDER_REMINDER_WINDOW_START_HOUR=0, ORDER_REMINDER_WINDOW_END_HOUR=24,  # no quiet hours unless a test wants them
    ORDER_WARM_SCORE=20,  # bare name+email test drafts count as WARM so they follow the standard 24h/72h/... ladder
)


def hours_ago(h):
    return timezone.now() - timedelta(hours=h)


def subjects():
    return [m.subject for m in mail.outbox]


def client_mails(addr):
    return [m for m in mail.outbox if m.to == [addr]]


@ENG
class EngagementBase(TestCase):
    def setUp(self):
        cache.clear()
        self.c = APIClient()

    def draft(self, idle_hours=0, **kw):
        kw.setdefault("service", "SPSS Data Analysis Help")
        kw.setdefault("service_slug", "spss-data-analysis-help")
        kw.setdefault("site", "thesis")
        with self.captureOnCommitCallbacks(execute=False):
            o = Order.objects.create(**kw)
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(idle_hours))
        o.refresh_from_db()
        return o

    def start(self):
        r = self.c.post("/api/orders/start/", {"service": "SPSS Data Analysis Help", "site": "thesis"}, format="json")
        return r.json()["token"]

    def patch(self, token, **body):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.c.patch(f"/api/orders/{token}/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r

    def run_engine(self, **kw):
        return engine.run_engine(**kw)


class IntentAlertTests(EngagementBase):
    def test_alert_fires_once_when_name_and_email_captured(self):
        token = self.start()
        self.assertEqual(len(mail.outbox), 0)
        self.patch(token, name="Jane Doe", email="jane@example.com", phone="+254700111222", step=1)
        intent = [m for m in mail.outbox if "ORDER STARTED" in m.subject]
        self.assertEqual(len(intent), 1)
        m = intent[0]
        self.assertEqual(set(m.to), set(ADMINS))
        self.assertIn("Jane Doe", m.subject)
        self.assertIn("jane@example.com", m.subject)
        self.assertIn("jane@example.com", m.alternatives[0][0])
        self.assertEqual(m.reply_to, ["jane@example.com"])
        self.patch(token, details="more text " * 10, step=2)   # later saves stay silent
        self.assertEqual(len([m for m in mail.outbox if "ORDER STARTED" in m.subject]), 1)
        eng = OrderEngagement.objects.get(order__public_token=token)
        self.assertIsNotNone(eng.intent_notified_at)
        self.assertGreater(eng.lead_score, 0)

    def test_no_alert_without_email(self):
        token = self.start()
        self.patch(token, name="No Email", step=1)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_without_name_waits_for_sweeper_grace(self):
        token = self.start()
        self.patch(token, email="late@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.run_engine()                      # still inside the 90s grace window
        self.assertEqual(len(mail.outbox), 0)
        Order.objects.filter(public_token=token).update(last_activity_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.run_engine().get("intent_sent"), 1)
        self.assertEqual(len([m for m in mail.outbox if "ORDER STARTED" in m.subject]), 1)
        self.run_engine()
        self.assertEqual(len([m for m in mail.outbox if "ORDER STARTED" in m.subject]), 1)

    def test_quick_order_gets_no_started_alert(self):
        r = self.c.post("/api/orders/quick/", {
            "site": "thesis", "name": "Quick Q", "email": "q@example.com", "service": "Regression Analysis Help",
            "details": "help"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(any("ORDER STARTED" in s for s in subjects()))
        self.assertEqual(len(mail.outbox), 2)  # unchanged: new-order alert + client confirmation
        self.assertEqual(OrderEngagement.objects.get().intent_skipped_reason, "quick_submit")

    def test_same_email_restart_is_deduped(self):
        for _ in range(2):
            token = self.start()
            self.patch(token, name="Repeat Rita", email="rita@example.com")
        self.assertEqual(len([m for m in mail.outbox if "ORDER STARTED" in m.subject]), 1)
        self.assertEqual(OrderEngagement.objects.filter(intent_skipped_reason="deduped").count(), 1)

    @override_settings(ORDER_INTENT_DEDUPE_HOURS=0)
    def test_attempt_number_and_history_for_returning_email(self):
        past = self.draft(50, name="Rita", email="rita@example.com")
        Order.objects.filter(pk=past.pk).update(created_at=hours_ago(60))
        token = self.start()
        self.patch(token, name="Rita", email="rita@example.com")
        m = next(m for m in mail.outbox if "ORDER STARTED" in m.subject)
        self.assertIn("attempt #2", m.subject)
        self.assertIn(past.reference, m.alternatives[0][0])

    def test_failed_send_releases_claim_and_retries(self):
        token = self.start()
        with mock.patch.object(notify, "_send_admin_engagement", side_effect=RuntimeError("smtp down")):
            self.patch(token, name="Flaky Fred", email="fred@example.com")
        eng = OrderEngagement.objects.get(order__public_token=token)
        self.assertIsNone(eng.intent_notified_at)
        self.assertEqual(eng.intent_failures, 1)
        Order.objects.filter(public_token=token).update(last_activity_at=hours_ago(1))
        self.assertEqual(self.run_engine().get("intent_sent"), 1)

    @override_settings(ORDER_INTENT_ALERTS_ENABLED=False)
    def test_can_be_switched_off(self):
        token = self.start()
        self.patch(token, name="Quiet Quinn", email="q@example.com")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(ORDER_ENGAGEMENT_ENABLED=False)
    def test_master_switch_disables_everything(self):
        token = self.start()
        self.patch(token, name="Off Olga", email="o@example.com")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.run_engine(), {"disabled": True})

    def test_old_orders_before_since_are_ignored(self):
        with override_settings(ORDER_ENGAGEMENT_SINCE=(timezone.now() + timedelta(hours=1)).isoformat()):
            token = self.start()
            self.patch(token, name="Old Ollie", email="ollie@example.com")
            self.assertEqual(len(mail.outbox), 0)


class SubmitInteractionTests(EngagementBase):
    def test_full_story_intent_then_submit_stops_reminders(self):
        token = self.start()
        self.patch(token, name="Sam Lee", email="sam@example.com", phone="+254711000111")
        o = Order.objects.get(public_token=token)
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(30))
        self.run_engine()
        self.assertEqual(len(client_mails("sam@example.com")), 1)       # reminder #1
        with self.captureOnCommitCallbacks(execute=True):
            r = self.c.post(f"/api/orders/{token}/submit/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        eng = OrderEngagement.objects.get(order=o)
        self.assertEqual(eng.stop_reason, "submitted")
        self.assertEqual(eng.recovered_by_stage, 1)
        admin_new = next(m for m in mail.outbox if o.reference in m.subject and "ORDER STARTED" not in m.subject and m.to == ADMINS)
        self.assertIn("RECOVERED by reminder #1", admin_new.body)
        before = len(mail.outbox)
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(200))
        self.run_engine()
        self.assertEqual(len(mail.outbox), before)


class ReminderLadderTests(EngagementBase):
    def test_cold_leads_get_a_slower_shorter_ladder(self):
        o = self.draft(30, name="Cold Cal", email="cal@example.com")
        with override_settings(ORDER_WARM_SCORE=90):
            self.assertEqual(OrderEngagement.for_order(o).lead_tier, "cold")
            score, tier, _ = intel.score_order(o)
            self.assertEqual(tier, "cold")
            ladder, urgent = intel.ladder_for(o, tier)
            self.assertEqual((ladder, urgent), ([36, 108, 252], False))
            self.run_engine()
            self.assertEqual(client_mails("cal@example.com"), [])          # 30h < 36h
            Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(37))
            self.run_engine()
            self.assertEqual(len(client_mails("cal@example.com")), 1)

    def test_nothing_before_first_offset(self):
        self.draft(10, name="Early Eve", email="eve@example.com")
        self.run_engine()
        self.assertEqual(client_mails("eve@example.com"), [])

    def test_first_reminder_content_and_compliance_headers(self):
        o = self.draft(26, name="Ann Mwangi", email="ann@example.com", phone="+254700000001")
        result = self.run_engine()
        self.assertEqual(result.get("reminders_sent"), 1)
        m = client_mails("ann@example.com")[0]
        self.assertIn("Ann", m.body)
        self.assertIn(f"https://api.example.com/api/orders/{o.public_token}/resume/?s=1", m.body)
        self.assertIn("List-Unsubscribe", m.extra_headers)
        self.assertEqual(m.extra_headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("/api/orders/unsubscribe/", m.extra_headers["List-Unsubscribe"])
        self.assertEqual(m.reply_to, ["info@scapedatasolutions.com"])
        log = OrderReminderLog.objects.get()
        self.assertEqual((log.stage, log.status), (1, "sent"))
        self.assertIn(log.variant, ("A", "B"))
        self.run_engine()                                   # idempotent
        self.assertEqual(len(client_mails("ann@example.com")), 1)

    def test_ladder_progresses_and_exhausts(self):
        o = self.draft(26, name="Ann", email="ann@example.com", phone="+254700000001")
        for idle, expected in ((26, 1), (80, 2), (180, 3), (350, 4)):
            Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(idle))
            OrderEngagement.objects.filter(order=o).update(last_reminder_at=hours_ago(idle - 20) if idle > 26 else None)
            with override_settings(ORDER_REMINDER_MAX_PER_EMAIL_WEEK=99):
                self.run_engine()
            self.assertEqual(len(client_mails("ann@example.com")), expected, f"after idle={idle}")
        subs = [m.subject for m in client_mails("ann@example.com")]
        self.assertTrue("Last reminder" in subs[-1] or "final note" in subs[-1], subs[-1])
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(400))
        OrderEngagement.objects.filter(order=o).update(last_reminder_at=hours_ago(10))
        self.run_engine()
        self.assertEqual(len(client_mails("ann@example.com")), 4)
        self.assertEqual(OrderEngagement.objects.get(order=o).stop_reason, "exhausted")

    def test_urgent_deadline_uses_compressed_ladder_and_mentions_deadline(self):
        o = self.draft(5, name="Dee", email="dee@example.com", deadline=timezone.localdate() + timedelta(days=2))
        self.run_engine()
        m = client_mails("dee@example.com")[0]
        self.assertIn("deadline", (m.subject + m.body).lower())

    def test_passed_deadline_stops_reminders(self):
        o = self.draft(30, name="Late", email="late@example.com")
        Order.objects.filter(pk=o.pk).update(deadline=timezone.localdate() - timedelta(days=1))
        self.run_engine()
        self.assertEqual(client_mails("late@example.com"), [])
        self.assertEqual(OrderEngagement.objects.get(order=o).stop_reason, "deadline_passed")

    def test_quiet_hours_defer_to_morning(self):
        o = self.draft(0, name="Night", email="night@example.com", country="Kenya")
        tz = intel.infer_timezone(o)
        self.assertEqual(str(tz), "Africa/Nairobi")
        local_3am = timezone.now().astimezone(tz).replace(hour=3, minute=0, second=0, microsecond=0)
        with override_settings(ORDER_REMINDER_WINDOW_START_HOUR=8, ORDER_REMINDER_WINDOW_END_HOUR=20):
            due = intel.next_send_time(local_3am, tz, salt=o.pk)
            self.assertEqual(due.astimezone(tz).hour, 8)
            self.assertGreaterEqual(due, local_3am)
            local_noon = local_3am.replace(hour=12)
            self.assertEqual(intel.next_send_time(local_noon, tz, salt=1), local_noon)
            local_9pm = local_3am.replace(hour=21)
            self.assertEqual(intel.next_send_time(local_9pm, tz, salt=1).astimezone(tz).date(),
                             (local_9pm + timedelta(days=1)).date())

    def test_timezone_inference_priority(self):
        self.assertEqual(str(intel.infer_timezone(Order(country="United Kingdom"))), "Europe/London")
        self.assertEqual(str(intel.infer_timezone(Order(phone="+254700000001"))), "Africa/Nairobi")
        self.assertEqual(str(intel.infer_timezone(Order(phone="+14155550100"))), "America/New_York")
        self.assertEqual(str(intel.infer_timezone(Order())), "Africa/Nairobi")

    def test_returning_client_resets_the_ladder(self):
        o = self.draft(26, name="Rae", email="rae@example.com")
        self.run_engine()
        self.assertEqual(len(client_mails("rae@example.com")), 1)
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(30))
        OrderEngagement.objects.filter(order=o).update(last_reminder_at=hours_ago(40))
        self.run_engine()
        self.assertEqual(len(client_mails("rae@example.com")), 2)
        self.assertEqual(client_mails("rae@example.com")[-1].extra_headers.get("List-Unsubscribe") is not None, True)
        self.assertEqual(OrderReminderLog.objects.latest("sent_at").stage, 1)      # back to stage 1, not stage 2

    def test_weekly_frequency_cap_per_email(self):
        for i in range(3):
            OrderReminderLog.objects.create(order=self.draft(500, email="cap@example.com"), email="cap@example.com", stage=1)
        o = self.draft(30, name="Cap", email="cap@example.com")
        self.run_engine()
        self.assertEqual(client_mails("cap@example.com"), [])

    def test_only_newest_draft_per_email_is_reminded(self):
        old = self.draft(60, name="Dup", email="dup@example.com")
        new = self.draft(30, name="Dup", email="dup@example.com")
        self.run_engine()
        self.assertEqual(len(client_mails("dup@example.com")), 1)
        self.assertEqual(OrderEngagement.objects.get(order=old).stop_reason, "superseded")

    def test_converted_person_not_reminded(self):
        self.draft(30, name="Bo", email="bo@example.com")
        Order.objects.create(name="Bo", email="bo@example.com", service="x", status="submitted", submitted_at=timezone.now())
        self.run_engine()
        self.assertEqual(client_mails("bo@example.com"), [])

    def test_paused_order_gets_nothing(self):
        o = self.draft(30, name="Pa", email="pa@example.com")
        OrderEngagement.for_order(o)
        OrderEngagement.objects.filter(order=o).update(reminders_paused=True)
        self.run_engine()
        self.assertEqual(client_mails("pa@example.com"), [])

    def test_failed_send_rolls_back_and_backs_off(self):
        o = self.draft(30, name="Fa", email="fa@example.com")
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=RuntimeError("provider down")):
            res = self.run_engine()
        self.assertEqual(res.get("reminders_failed"), 1)
        eng = OrderEngagement.objects.get(order=o)
        self.assertEqual((eng.reminders_sent, eng.lifetime_reminders, eng.send_failures), (0, 0, 1))
        self.assertGreater(eng.next_reminder_at, timezone.now())
        self.run_engine()                                    # inside backoff: no retry
        self.assertEqual(OrderReminderLog.objects.filter(status="failed").count(), 1)
        OrderEngagement.objects.filter(order=o).update(next_reminder_at=timezone.now() - timedelta(minutes=1))
        self.run_engine()
        self.assertEqual(len(client_mails("fa@example.com")), 1)

    def test_dry_run_sends_and_writes_nothing(self):
        self.draft(30, name="Dry", email="dry@example.com")
        out = self.run_engine(dry_run=True)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(OrderReminderLog.objects.count(), 0)
        self.assertTrue(any("send:due" in n for n in out["notes"]))

    def test_management_command_dry_run(self):
        self.draft(30, name="Cmd", email="cmd@example.com")
        call_command("run_order_engagement", "--dry-run")
        self.assertEqual(len(mail.outbox), 0)

    def test_ab_variants_use_different_subjects(self):
        o = self.draft(1, name="Vee", email="vee@example.com")
        a = notify.build_reminder_copy(o, 1, 4, "A", False)[1]
        b = notify.build_reminder_copy(o, 1, 4, "B", False)[1]
        self.assertNotEqual(a, b)


class SuppressionAndUnsubscribeTests(EngagementBase):
    def test_suppressed_email_gets_no_reminder(self):
        EmailSuppression.add("Nope@Example.com", "bounced")
        self.draft(30, name="No", email="nope@example.com")
        self.run_engine()
        self.assertEqual(client_mails("nope@example.com"), [])

    def test_get_shows_confirmation_but_does_not_unsubscribe(self):
        tok = notify.unsubscribe_token("u@example.com")
        r = self.c.get(f"/api/orders/unsubscribe/{tok}/", HTTP_ACCEPT="text/html")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Yes, unsubscribe me")
        self.assertFalse(EmailSuppression.objects.exists())

    def test_post_unsubscribes_and_stops_ladder(self):
        o = self.draft(30, name="U", email="u@example.com")
        tok = notify.unsubscribe_token("U@example.com")
        r = self.c.post(f"/api/orders/unsubscribe/{tok}/", "List-Unsubscribe=One-Click",
                        content_type="application/x-www-form-urlencoded")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(EmailSuppression.reason_for("u@example.com"), "unsubscribed")
        self.run_engine()
        self.assertEqual(client_mails("u@example.com"), [])

    def test_tampered_token_rejected(self):
        tok = notify.unsubscribe_token("u@example.com")[:-3] + "xxx"
        self.assertEqual(self.c.post(f"/api/orders/unsubscribe/{tok}/").status_code, 400)
        self.assertFalse(EmailSuppression.objects.exists())

    def test_legacy_recovery_email_respects_suppression(self):
        EmailSuppression.add("ann@example.com", "unsubscribed")
        Order.objects.create(service="x", name="Ann", email="ann@example.com", phone="+254700000001")
        Order.objects.update(last_activity_at=hours_ago(1))
        services.sweep_abandoned_orders()
        self.assertEqual(client_mails("ann@example.com"), [])
        self.assertTrue(any("[ABANDONED ORDER" in s for s in subjects()))

    def test_hard_bounce_event_suppresses_but_soft_bounce_does_not(self):
        ev = mock.Mock(event_type="bounced", recipient="hb@example.com", esp_event={"event": "hard_bounce"})
        engagement_hooks.on_anymail_event(None, ev, "Brevo")
        self.assertEqual(EmailSuppression.reason_for("hb@example.com"), "bounced")
        soft = mock.Mock(event_type="bounced", recipient="sb@example.com", esp_event={"event": "soft_bounce"})
        engagement_hooks.on_anymail_event(None, soft, "Brevo")
        self.assertEqual(EmailSuppression.reason_for("sb@example.com"), "")
        spam = mock.Mock(event_type="complained", recipient="sp@example.com", esp_event={"event": "spam"})
        engagement_hooks.on_anymail_event(None, spam, "Brevo")
        self.assertEqual(EmailSuppression.reason_for("sp@example.com"), "complained")


class ExitSignalTests(EngagementBase):
    def test_leave_beacon_triggers_early_abandoned_alert_then_recovery_at_30_min(self):
        token = self.start()
        self.patch(token, name="Lee Ave", email="lee@example.com", phone="+254722000333", step=2)
        r = self.c.post(f"/api/orders/{token}/leave/")
        self.assertEqual(r.status_code, 204)
        o = Order.objects.get(public_token=token)
        mail.outbox.clear()
        self.run_engine()
        self.assertEqual(mail.outbox, [])  # inside the 5-minute grace
        Order.objects.filter(pk=o.pk).update(last_activity_at=timezone.now() - timedelta(minutes=10))
        OrderEngagement.objects.filter(order=o).update(exit_signalled_at=timezone.now() - timedelta(minutes=8))
        self.run_engine()
        self.assertEqual(len([s for s in subjects() if "[ABANDONED ORDER" in s]), 1)
        self.assertEqual(client_mails("lee@example.com"), [])             # too early for the client nudge
        services.sweep_abandoned_orders()                                   # legacy sweeper must not double-alert
        self.assertEqual(len([s for s in subjects() if "[ABANDONED ORDER" in s]), 1)
        Order.objects.filter(pk=o.pk).update(last_activity_at=timezone.now() - timedelta(minutes=40))
        OrderEngagement.objects.filter(order=o).update(exit_signalled_at=timezone.now() - timedelta(minutes=35))
        self.run_engine()
        self.assertEqual(len(client_mails("lee@example.com")), 1)
        self.run_engine()
        self.assertEqual(len(client_mails("lee@example.com")), 1)

    def test_coming_back_cancels_the_exit_signal(self):
        token = self.start()
        self.patch(token, name="Back Bea", email="bea@example.com", phone="+254722000444")
        self.c.post(f"/api/orders/{token}/leave/")
        self.assertIsNotNone(OrderEngagement.objects.get().exit_signalled_at)
        self.c.get(f"/api/orders/{token}/")          # loading the draft = they're back
        self.assertIsNone(OrderEngagement.objects.get().exit_signalled_at)
        self.c.post(f"/api/orders/{token}/leave/")
        self.c.post(f"/api/orders/{token}/return/")
        self.assertIsNone(OrderEngagement.objects.get().exit_signalled_at)

    def test_unknown_token_leave_is_204(self):
        self.assertEqual(self.c.post("/api/orders/nope/leave/").status_code, 204)


class ClickAndEscalationTests(EngagementBase):
    def _reminded(self):
        o = self.draft(30, name="Cla Rke", email="clark@example.com", phone="+254733000555")
        self.run_engine()
        OrderReminderLog.objects.update(sent_at=hours_ago(1))
        return o

    def test_click_is_tracked_and_redirects_to_resume_url(self):
        o = self._reminded()
        r = self.c.get(f"/api/orders/{o.public_token}/resume/?s=1&v=A", HTTP_USER_AGENT="Mozilla/5.0 iPhone")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"resume={o.public_token}", r["Location"])
        self.assertEqual(OrderReminderLog.objects.get().click_count, 1)
        self.assertEqual(OrderEngagement.objects.get(order=o).clicks, 1)

    def test_scanner_clicks_are_not_counted(self):
        o = self._reminded()
        self.c.get(f"/api/orders/{o.public_token}/resume/?s=1", HTTP_USER_AGENT="Mimecast Link Scanner")
        self.c.head(f"/api/orders/{o.public_token}/resume/?s=1", HTTP_USER_AGENT="Mozilla/5.0")
        OrderReminderLog.objects.update(sent_at=timezone.now())          # "instant" click right after send
        self.c.get(f"/api/orders/{o.public_token}/resume/?s=1", HTTP_USER_AGENT="Mozilla/5.0")
        self.assertEqual(OrderReminderLog.objects.get().click_count, 0)

    def test_unknown_resume_token_still_redirects(self):
        self.assertEqual(self.c.get("/api/orders/nope/resume/").status_code, 302)

    def test_clicked_but_not_finished_escalates_once(self):
        o = self._reminded()
        self.c.get(f"/api/orders/{o.public_token}/resume/?s=1", HTTP_USER_AGENT="Mozilla/5.0")
        OrderEngagement.objects.filter(order=o).update(last_clicked_at=hours_ago(3))
        Order.objects.filter(pk=o.pk).update(last_activity_at=hours_ago(31))
        mail.outbox.clear()
        self.run_engine()
        self.assertEqual(len([s for s in subjects() if "WARM SIGNAL" in s]), 1)
        self.run_engine()
        self.assertEqual(len([s for s in subjects() if "WARM SIGNAL" in s]), 1)

    def test_hot_lead_escalates_after_a_day(self):
        o = self.draft(
            30, name="Hot Hana", email="hana@uonbi.ac.ke", phone="+254733000999", topic="Impact of X on Y",
            details="A very detailed brief about the study, the dataset and the models required. " * 4,
            current_step=5, deadline=timezone.localdate() + timedelta(days=6),
        )
        mail.outbox.clear()
        self.run_engine()
        eng = OrderEngagement.objects.get(order=o)
        self.assertEqual(eng.lead_tier, "hot", eng.score_breakdown)
        self.assertEqual(len([s for s in subjects() if "HOT LEAD PENDING" in s]), 1)

    def test_scoring_penalises_disposable_email(self):
        o = self.draft(1, name="Spam", email="x@mailinator.com")
        score, tier, _ = intel.score_order(o)
        self.assertEqual(tier, "cold")
        self.assertEqual(score, 0)  # service 8 + name 2 - 40 (disposable) floors at 0


class DigestTests(EngagementBase):
    def test_digest_lists_open_leads_and_skips_when_empty(self):
        self.assertEqual(notify.send_digest(), "empty")
        self.draft(30, name="Dig Dan", email="dan@example.com", phone="+254700123123")
        self.run_engine()
        mail.outbox.clear()
        self.assertEqual(notify.send_digest(), "sent")
        m = mail.outbox[0]
        self.assertEqual(set(m.to), set(ADMINS))
        self.assertIn("dan@example.com", m.body)
        self.assertIn("[ORDER DIGEST]", m.subject)


class SystemCheckTests(TestCase):
    @override_settings(ORDER_ENGAGEMENT_ENABLED=True, ORDERS_API_BASE_URL="")
    def test_warns_when_api_base_missing(self):
        from django.core import checks
        ids = [m.id for m in checks.run_checks()]
        self.assertIn("orders.W001", ids)
