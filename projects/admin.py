from django.contrib import admin

from .models import Project, ProjectMessage, ProjectStatusChange


class ProjectMessageInline(admin.TabularInline):
    model = ProjectMessage
    extra = 0
    fields = ("sender_type", "sender_name", "sender_user", "body", "created_at")
    readonly_fields = ("created_at",)


class ProjectStatusChangeInline(admin.TabularInline):
    model = ProjectStatusChange
    extra = 0
    fields = ("from_status", "to_status", "changed_by", "note", "created_at")
    readonly_fields = ("from_status", "to_status", "changed_by", "note", "created_at")
    can_delete = False
    max_num = 0  # read-only log — status changes must go through services.change_project_status()


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "status", "budget", "currency", "assigned_admin", "contract", "created_at")
    list_filter = ("status", "currency", "created_at")
    search_fields = ("title", "description", "owner__username", "owner__email")
    readonly_fields = ("id", "contract", "attachment_url", "attachment_filename", "created_at", "updated_at")
    date_hierarchy = "created_at"
    inlines = [ProjectStatusChangeInline, ProjectMessageInline]


@admin.register(ProjectMessage)
class ProjectMessageAdmin(admin.ModelAdmin):
    list_display = ("project", "sender_type", "sender_name", "created_at", "read_by_admin_at", "read_by_client_at")
    list_filter = ("sender_type", "created_at")
    search_fields = ("project__title", "body", "sender_name")
    readonly_fields = ("created_at",)


@admin.register(ProjectStatusChange)
class ProjectStatusChangeAdmin(admin.ModelAdmin):
    list_display = ("project", "from_status", "to_status", "changed_by", "created_at")
    list_filter = ("from_status", "to_status", "created_at")
    search_fields = ("project__title", "note")
    readonly_fields = ("project", "changed_by", "from_status", "to_status", "note", "created_at")

    def has_add_permission(self, request):
        # Append-only audit log — rows are only ever created by
        # services.change_project_status(), never by hand.
        return False

    def has_change_permission(self, request, obj=None):
        return False
