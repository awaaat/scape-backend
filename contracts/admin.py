from django.contrib import admin

from .models import Contract, ContractRevision, Message, Milestone


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


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        "title", "client_name", "client_email", "status", "total_value", "currency",
        "sent_at", "signed_at", "created_at",
    )
    list_filter = ("status", "currency", "esign_method", "created_at")
    search_fields = ("title", "client_name", "client_email", "client_company")
    readonly_fields = (
        "id", "access_token_hash", "access_token_expires_at",
        "signed_content_hash", "signed_ip", "signed_user_agent",
        "created_at", "updated_at",
    )
    date_hierarchy = "created_at"
    inlines = [MilestoneInline, ContractRevisionInline, MessageInline]


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
