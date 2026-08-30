from django.contrib import admin, messages

from . import services
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
    actions = ["convert_to_contract"]

    def save_model(self, request, obj, form, change):
        """
        Routes status changes through services.change_project_status()
        instead of a bare save() — that's what writes the
        ProjectStatusChange audit row and enforces
        Project.can_transition_to(), same as the client-facing API path
        (AdminProjectDetailView.patch). Editing any OTHER field still
        just saves normally.
        """
        if change and "status" in form.changed_data:
            old_status = Project.objects.get(pk=obj.pk).status
            new_status = obj.status
            obj.status = old_status  # revert so change_project_status does the transition check + save
            try:
                services.change_project_status(
                    project=obj, new_status=new_status, changed_by=request.user,
                    note="Changed via Django admin.",
                )
            except services.ProjectConversionError as exc:
                self.message_user(request, str(exc), level=messages.ERROR)
                return
            # Save any other changed fields the admin also edited in the same form.
            other_fields = [f for f in form.changed_data if f != "status"]
            if other_fields:
                obj.save(update_fields=other_fields + ["updated_at"])
        else:
            super().save_model(request, obj, form, change)

    @admin.action(description="Convert selected projects to contracts")
    def convert_to_contract(self, request, queryset):
        """
        Calls services.convert_project_to_contract() for each selected
        project — creates the Contract, links it back via
        project.contract, and moves the project to 'in_progress'. Skips
        (with a message) any project that's already been converted,
        since convert_project_to_contract() raises rather than
        silently returning the existing link, and a bulk action
        shouldn't blow up entirely because one row was already done.
        """
        converted, skipped = 0, []
        for project in queryset:
            try:
                services.convert_project_to_contract(project, created_by=request.user)
                converted += 1
            except services.ProjectConversionError as exc:
                skipped.append(f"{project.title}: {exc}")

        if converted:
            self.message_user(
                request, f"Converted {converted} project(s) to contracts.", level=messages.SUCCESS,
            )
        for note in skipped:
            self.message_user(request, note, level=messages.WARNING)


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
