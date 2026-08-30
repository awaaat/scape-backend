from django.contrib import admin, messages

from . import services
from .models import (
    Contract, ContractChangeRequest, ContractDispute, ContractRevision, Invoice, Message, Milestone,
    MilestoneDeliverable, MilestoneEvent,
)


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 0
    fields = (
        "title", "amount", "currency", "order", "due_date", "status",
        "paystack_reference", "funded_at", "submitted_at", "released_at",
    )
    readonly_fields = ("paystack_reference", "funded_at", "submitted_at", "released_at")
    show_change_link = True


class ContractRevisionInline(admin.TabularInline):
    model = ContractRevision
    extra = 0
    fields = ("version_number", "title", "total_value", "currency", "edited_by", "note", "created_at")
    readonly_fields = ("version_number", "title", "total_value", "currency", "edited_by", "note", "created_at")
    can_delete = False


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("sender_type", "sender_name", "sender_user", "body", "created_at")
    readonly_fields = ("created_at",)


class ContractChangeRequestInline(admin.TabularInline):
    model = ContractChangeRequest
    extra = 0
    fields = ("status", "requested_by_name", "reason", "admin_note", "reviewed_by", "created_at")
    readonly_fields = ("status", "requested_by_name", "reason", "admin_note", "reviewed_by", "created_at")
    can_delete = False
    max_num = 0  # decide via services.decide_change_request() / the admin API, not by hand


class ContractDisputeInline(admin.TabularInline):
    """Shows both contract-wide and milestone-scoped disputes filed on
    this contract — `milestone` is blank for the former (see
    ContractDispute's model docstring)."""
    model = ContractDispute
    extra = 0
    fields = ("status", "category", "milestone", "raised_by_name", "description", "resolution_note", "created_at")
    readonly_fields = (
        "status", "category", "milestone", "raised_by_name", "description", "resolution_note", "created_at",
    )
    can_delete = False
    max_num = 0  # resolve via services.resolve_dispute() / the admin API, not by hand


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        "title", "client_name", "client_email", "status", "total_value", "currency",
        "has_open_dispute_flag", "has_unsigned_required_agreements_flag", "sent_at", "signed_at", "created_at",
    )
    list_filter = ("status", "currency", "esign_method", "created_at")
    search_fields = ("title", "client_name", "client_email", "client_company")
    readonly_fields = (
        "id", "access_token_hash", "access_token_expires_at",
        "signed_content_hash", "signed_ip", "signed_user_agent",
        "created_at", "updated_at",
    )
    date_hierarchy = "created_at"
    inlines = [MilestoneInline, ContractRevisionInline, ContractChangeRequestInline, ContractDisputeInline, MessageInline]
    actions = ["send_to_client"]

    @admin.display(boolean=True, description="Open dispute")
    def has_open_dispute_flag(self, obj):
        return obj.has_open_dispute

    @admin.display(boolean=True, description="Unsigned required agreements")
    def has_unsigned_required_agreements_flag(self, obj):
        return obj.has_unsigned_required_agreements

    @admin.action(description="Send contract to client (email invite link)")
    def send_to_client(self, request, queryset):
        sent = 0
        for contract in queryset:
            services.send_contract_to_client(contract)
            sent += 1
        self.message_user(request, f"Sent {sent} contract(s) to their clients.", level=messages.SUCCESS)


class MilestoneDeliverableInline(admin.TabularInline):
    """Read-only — created exclusively by
    services.submit_milestone_deliverable(), never by hand (see that
    model's docstring)."""
    model = MilestoneDeliverable
    extra = 0
    fields = ("version", "summary", "file_url", "file_filename", "link_url", "submitted_by", "created_at")
    readonly_fields = ("version", "summary", "file_url", "file_filename", "link_url", "submitted_by", "created_at")
    can_delete = False
    max_num = 0


class MilestoneEventInline(admin.TabularInline):
    """Append-only audit log — never edited or deleted (see that
    model's docstring)."""
    model = MilestoneEvent
    extra = 0
    fields = ("event_type", "actor_type", "actor_user", "actor_name", "metadata", "created_at")
    readonly_fields = ("event_type", "actor_type", "actor_user", "actor_name", "metadata", "created_at")
    can_delete = False
    max_num = 0


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = (
        "title", "contract", "amount", "currency", "status", "due_date",
        "funded_at", "submitted_at", "auto_release_at", "released_at", "has_open_dispute_flag",
    )
    list_filter = ("status", "currency", "auto_released")
    search_fields = ("title", "contract__title", "contract__client_email", "paystack_reference")
    readonly_fields = (
        "paystack_reference", "invoiced_at", "funded_at", "submitted_at", "auto_release_at",
        "released_at", "auto_released", "released_by_user", "created_at", "updated_at",
    )
    inlines = [MilestoneDeliverableInline, MilestoneEventInline]
    actions = ["create_invoice_for_selected"]

    @admin.display(boolean=True, description="Open dispute")
    def has_open_dispute_flag(self, obj):
        return obj.has_open_dispute

    @admin.action(description="Create invoice for selected milestone(s)")
    def create_invoice_for_selected(self, request, queryset):
        """
        Runs one milestone at a time — create_invoice() bundles in any
        earlier unbilled milestone on the same contract automatically
        (see Invoice's model docstring), so selecting milestone 3 alone
        still correctly pulls in 1 and 2 if they're still pending.
        """
        created, skipped = 0, []
        for milestone in queryset:
            try:
                services.create_invoice(milestone, created_by=request.user)
                created += 1
            except services.InvoiceError as exc:
                skipped.append(f"{milestone.title}: {exc}")

        if created:
            self.message_user(request, f"Created {created} invoice(s).", level=messages.SUCCESS)
        for note in skipped:
            self.message_user(request, note, level=messages.WARNING)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number", "contract", "total_amount", "currency", "status",
        "issued_at", "due_at", "sent_at", "paid_at",
    )
    list_filter = ("status", "currency", "is_manual_payment", "issued_at")
    search_fields = ("invoice_number", "contract__title", "contract__client_email")
    readonly_fields = (
        "id", "invoice_number", "total_amount", "issued_at", "created_by", "sent_at",
        "sent_via_email", "sent_via_message", "pdf_url", "paystack_reference", "paid_at",
        "manual_payment_recorded_by", "created_at", "updated_at",
    )
    date_hierarchy = "issued_at"
    actions = ["send_selected", "void_selected"]

    def has_add_permission(self, request):
        # Created only via services.create_invoice() (see MilestoneAdmin's
        # "Create invoice" action), never by hand — an invoice must
        # always be seeded from real pending milestones.
        return False

    @admin.action(description="Send selected invoice(s) to client (renders PDF + emails)")
    def send_selected(self, request, queryset):
        sent, failed = 0, []
        for invoice in queryset:
            try:
                services.send_invoice(invoice, sent_by=request.user)
                sent += 1
            except services.InvoiceError as exc:
                failed.append(f"{invoice.invoice_number}: {exc}")

        if sent:
            self.message_user(request, f"Sent {sent} invoice(s).", level=messages.SUCCESS)
        for note in failed:
            self.message_user(request, note, level=messages.WARNING)

    @admin.action(description="Void selected invoice(s)")
    def void_selected(self, request, queryset):
        voided, failed = 0, []
        for invoice in queryset:
            try:
                services.void_invoice(invoice, void_reason="Voided via Django admin.", voided_by=request.user)
                voided += 1
            except services.InvoiceError as exc:
                failed.append(f"{invoice.invoice_number}: {exc}")

        if voided:
            self.message_user(request, f"Voided {voided} invoice(s).", level=messages.SUCCESS)
        for note in failed:
            self.message_user(request, note, level=messages.WARNING)


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("contract", "sender_type", "sender_name", "created_at", "read_by_admin_at", "read_by_client_at")
    list_filter = ("sender_type", "created_at")
    search_fields = ("contract__title", "contract__client_email", "body", "sender_name")
    readonly_fields = ("created_at",)


@admin.register(ContractChangeRequest)
class ContractChangeRequestAdmin(admin.ModelAdmin):
    list_display = ("contract", "status", "requested_by_name", "reviewed_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("contract__title", "reason", "requested_by_name")
    readonly_fields = (
        "contract", "requested_by_user", "requested_by_name", "proposed_title",
        "proposed_scope_of_work", "proposed_total_value", "proposed_currency", "reason",
        "created_at",
    )

    def has_add_permission(self, request):
        # Filed only by clients via the API — see services.request_contract_change().
        return False


@admin.register(ContractDispute)
class ContractDisputeAdmin(admin.ModelAdmin):
    """`milestone` is blank for a contract-wide dispute — see this
    model's docstring for the two scoping modes."""
    list_display = ("contract", "milestone", "status", "category", "raised_by_name", "resolved_by", "created_at")
    list_filter = ("status", "category", "created_at")
    search_fields = ("contract__title", "description", "raised_by_name")
    readonly_fields = (
        "contract", "milestone", "raised_by_user", "raised_by_name", "category", "description", "created_at",
    )

    def has_add_permission(self, request):
        # Filed only by clients via the API — see services.raise_dispute().
        return False