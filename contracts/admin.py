from django.contrib import admin

from .models import Contract, ContractChangeRequest, ContractDispute, ContractRevision, Message, Milestone


class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 0
    fields = ("title", "amount", "currency", "order", "due_date", "status", "paystack_reference", "paid_at")
    readonly_fields = ("paystack_reference", "paid_at")


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
    model = ContractDispute
    extra = 0
    fields = ("status", "category", "raised_by_name", "description", "resolution_note", "created_at")
    readonly_fields = ("status", "category", "raised_by_name", "description", "resolution_note", "created_at")
    can_delete = False
    max_num = 0  # resolve via services.resolve_dispute() / the admin API, not by hand


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        "title", "client_name", "client_email", "status", "total_value", "currency",
        "has_open_dispute_flag", "sent_at", "signed_at", "created_at",
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

    @admin.display(boolean=True, description="Open dispute")
    def has_open_dispute_flag(self, obj):
        return obj.has_open_dispute


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ("title", "contract", "amount", "currency", "status", "due_date", "paid_at")
    list_filter = ("status", "currency")
    search_fields = ("title", "contract__title", "contract__client_email", "paystack_reference")
    readonly_fields = ("paystack_reference", "invoiced_at", "paid_at", "created_at", "updated_at")


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
    list_display = ("contract", "status", "category", "raised_by_name", "resolved_by", "created_at")
    list_filter = ("status", "category", "created_at")
    search_fields = ("contract__title", "description", "raised_by_name")
    readonly_fields = ("contract", "raised_by_user", "raised_by_name", "category", "description", "created_at")

    def has_add_permission(self, request):
        # Filed only by clients via the API — see services.raise_dispute().
        return False
