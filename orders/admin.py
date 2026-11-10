from django.contrib import admin, messages
from django.utils.html import format_html

from . import emails
from .models import Order, OrderFile


class OrderFileInline(admin.TabularInline):
    model = OrderFile
    extra = 0
    can_delete = True
    fields = ("original_name", "size_bytes", "link")
    readonly_fields = ("original_name", "size_bytes", "link")

    def link(self, obj):
        return format_html('<a href="{}" target="_blank" rel="noopener">Open</a>', obj.url) if obj.pk else ""

    def has_add_permission(self, request, obj=None):
        return False


def _set_status(status):
    def action(modeladmin, request, queryset):
        n = queryset.update(status=status)
        modeladmin.message_user(request, f"{n} order(s) marked '{status}'.", messages.SUCCESS)
    action.short_description = f"Mark selected as: {status}"
    action.__name__ = f"mark_{status}"
    return action


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "reference", "site", "status", "display_name", "email", "whatsapp", "service",
        "deadline", "days_left_col", "files_col", "last_activity_at",
    )
    list_filter = ("status", "site", "service_slug", "academic_level", "software", "submitted_at")
    search_fields = ("reference", "name", "email", "phone", "service", "topic", "details")
    date_hierarchy = "last_activity_at"
    list_select_related = ("visitor",)
    list_per_page = 50
    inlines = [OrderFileInline]
    actions = [
        _set_status("contacted"), _set_status("quoted"), _set_status("won"),
        _set_status("lost"), _set_status("spam"), "resend_admin_email",
    ]
    readonly_fields = (
        "reference", "public_token", "visitor", "created_at", "updated_at", "last_activity_at",
        "submitted_at", "current_step", "admin_notified", "confirmation_sent",
        "abandoned_notified", "recovery_sent", "package",
    )
    fieldsets = (
        ("Order package (copy-paste friendly)", {"fields": ("package",)}),
        ("Pipeline", {"fields": ("site", "status", "admin_notes")}),
        ("Contact", {"fields": ("name", "email", "phone", "country")}),
        ("Order", {"fields": (
            "service_slug", "service", "academic_level", "data_status", "software",
            "deadline_window", "deadline", "topic", "details", "extra_answers",
        )}),
        ("Funnel and attribution", {"fields": (
            "reference", "public_token", "current_step", "source_url", "referrer", "utm", "visitor",
            "created_at", "last_activity_at", "submitted_at",
        )}),
        ("Notifications", {"fields": ("admin_notified", "confirmation_sent", "abandoned_notified", "recovery_sent")}),
    )

    @admin.display(description="WhatsApp")
    def whatsapp(self, obj):
        if not obj.whatsapp_link:
            return "-"
        return format_html('<a href="{}" target="_blank" rel="noopener">{}</a>', obj.whatsapp_link, obj.phone)

    @admin.display(description="Due")
    def days_left_col(self, obj):
        d = obj.days_left
        if d is None:
            return "-"
        color = "#b42318" if d <= 3 else "inherit"
        return format_html('<span style="color:{};font-weight:600">{}d</span>', color, d)

    @admin.display(description="Files")
    def files_col(self, obj):
        return obj.files.count()

    @admin.display(description="Package")
    def package(self, obj):
        return format_html('<pre style="white-space:pre-wrap;margin:0">{}</pre>', obj.as_text()) if obj.pk else "-"

    @admin.action(description="Re-send the admin notification email")
    def resend_admin_email(self, request, queryset):
        sent = 0
        for order in queryset.exclude(status=Order.Status.DRAFT):
            emails.send_admin_email(order, "submitted")
            sent += 1
        self.message_user(request, f"Re-sent {sent} notification(s).", messages.SUCCESS)


# ═══ ORDER ENGAGEMENT LAYER (install_order_engagement.sh) ═══
from django.utils import timezone as _tz  # noqa: E402

from . import engagement_intel as _intel  # noqa: E402
from . import engagement_notifications as _notify  # noqa: E402
from .models import EmailSuppression, OrderEngagement, OrderReminderLog  # noqa: E402


class OrderEngagementInline(admin.StackedInline):
    model = OrderEngagement
    extra = 0
    max_num = 1
    can_delete = False
    verbose_name_plural = "Engagement, lead score and reminders"
    fields = (
        ("lead_tier", "lead_score"), "score_breakdown_display",
        "reminders_paused", ("reminders_sent", "lifetime_reminders"), ("last_reminder_at", "next_reminder_at"),
        "stop_reason", ("clicks", "last_clicked_at"), "recovered_by_stage",
        ("intent_notified_at", "intent_attempt_no", "intent_skipped_reason"), ("exit_signalled_at", "exit_signal_count"),
    )
    readonly_fields = (
        "lead_tier", "lead_score", "score_breakdown_display", "reminders_sent", "lifetime_reminders",
        "last_reminder_at", "next_reminder_at", "stop_reason", "clicks", "last_clicked_at", "recovered_by_stage",
        "intent_notified_at", "intent_attempt_no", "intent_skipped_reason", "exit_signalled_at", "exit_signal_count",
    )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Why this score")
    def score_breakdown_display(self, obj):
        if not obj or not obj.score_breakdown:
            return "-"
        return format_html(
            "<br>".join("<b>{}{}</b> &nbsp;{}" for _ in obj.score_breakdown),
            *[x for label, pts in obj.score_breakdown for x in ("+" if pts > 0 else "", pts, label)],
        )


class OrderReminderLogInline(admin.TabularInline):
    model = OrderReminderLog
    extra = 0
    can_delete = False
    fields = ("stage", "variant", "status", "subject", "sent_at", "clicked_at", "click_count", "error")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.display(description="Lead", ordering="engagement__lead_score")
def _lead_col(self, obj):
    eng = getattr(obj, "engagement", None)
    if eng is None or not eng.lead_score:
        return "-"
    color = {"hot": "#b42318", "warm": "#9a6b00"}.get(eng.lead_tier, "#6b7280")
    return format_html('<b style="color:{}">{} {}</b>', color, eng.lead_score, eng.get_lead_tier_display())


def _drafts_with_email(queryset):
    return queryset.filter(status=Order.Status.DRAFT).exclude(email="")


@admin.action(description="Pause automatic reminders")
def _pause_reminders(modeladmin, request, queryset):
    n = 0
    for order in queryset:
        OrderEngagement.objects.filter(pk=OrderEngagement.for_order(order).pk).update(reminders_paused=True)
        n += 1
    modeladmin.message_user(request, f"Paused reminders for {n} order(s).", messages.SUCCESS)


@admin.action(description="Resume automatic reminders")
def _resume_reminders(modeladmin, request, queryset):
    n = OrderEngagement.objects.filter(order__in=queryset).update(reminders_paused=False)
    modeladmin.message_user(request, f"Resumed reminders for {n} order(s).", messages.SUCCESS)


@admin.action(description="Send the next reminder now (draft orders)")
def _send_reminder_now(modeladmin, request, queryset):
    sent = skipped = 0
    for order in _drafts_with_email(queryset).select_related("visitor"):
        if EmailSuppression.reason_for(order.email):
            skipped += 1
            continue
        eng = OrderEngagement.for_order(order)
        ladder, urgent = _intel.ladder_for(order, eng.lead_tier)
        total = max(len(ladder), eng.reminders_sent + 1, 2)
        stage = eng.reminders_sent + 1
        log = _notify.send_reminder(order, eng, stage, total, urgent, _intel.ab_variant(order, stage))
        if log.status == OrderReminderLog.Status.SENT:
            OrderEngagement.objects.filter(pk=eng.pk).update(
                reminders_sent=stage, lifetime_reminders=eng.lifetime_reminders + 1, last_reminder_at=_tz.now(),
            )
            sent += 1
        else:
            skipped += 1
    modeladmin.message_user(request, f"Sent {sent} reminder(s); skipped {skipped}.", messages.SUCCESS)


OrderAdmin.inlines = [*OrderAdmin.inlines, OrderEngagementInline, OrderReminderLogInline]
OrderAdmin.lead_col = _lead_col
OrderAdmin.list_display = (*OrderAdmin.list_display, "lead_col")
OrderAdmin.list_filter = (*OrderAdmin.list_filter, "engagement__lead_tier")
OrderAdmin.list_select_related = ("visitor", "engagement")
OrderAdmin.actions = [*OrderAdmin.actions, _pause_reminders, _resume_reminders, _send_reminder_now]


@admin.register(OrderReminderLog)
class OrderReminderLogAdmin(admin.ModelAdmin):
    list_display = ("order", "email", "stage", "variant", "status", "sent_at", "clicked_at", "click_count")
    list_filter = ("status", "stage", "variant", "order__site")
    search_fields = ("email", "order__reference", "subject")
    date_hierarchy = "sent_at"
    readonly_fields = [f.name for f in OrderReminderLog._meta.fields]

    def has_add_permission(self, request):
        return False


@admin.register(EmailSuppression)
class EmailSuppressionAdmin(admin.ModelAdmin):
    list_display = ("email", "reason", "source", "created_at")
    list_filter = ("reason", "source")
    search_fields = ("email", "note")
