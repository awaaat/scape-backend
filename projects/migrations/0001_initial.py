from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('contracts', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Project',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('description', models.TextField()),
                ('category', models.CharField(blank=True, help_text="Free-text project type, e.g. 'Data collection', 'Web development'.", max_length=100)),
                ('budget', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('currency', models.CharField(default='KES', max_length=3)),
                ('deadline', models.DateField(blank=True, null=True)),
                ('attachment_url', models.URLField(blank=True, max_length=500)),
                ('attachment_filename', models.CharField(blank=True, max_length=255)),
                ('attachment_content_type', models.CharField(blank=True, max_length=100)),
                ('status', models.CharField(choices=[('submitted', 'Submitted'), ('under_review', 'Under review'), ('in_progress', 'In progress'), ('completed', 'Completed'), ('cancelled', 'Cancelled')], db_index=True, default='submitted', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('assigned_admin', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assigned_projects', to=settings.AUTH_USER_MODEL)),
                ('contract', models.OneToOneField(blank=True, help_text='Set once this project has been formalized into a Contract (see services.convert_project_to_contract). Milestones and payments live on the Contract from that point on, not here.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='source_project', to='contracts.contract')),
                ('owner', models.ForeignKey(help_text='The client who submitted this project.', on_delete=django.db.models.deletion.CASCADE, related_name='projects', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ProjectStatusChange',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_status', models.CharField(max_length=20)),
                ('to_status', models.CharField(max_length=20)),
                ('note', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('changed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='project_status_changes', to=settings.AUTH_USER_MODEL)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='status_changes', to='projects.project')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ProjectMessage',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sender_type', models.CharField(choices=[('admin', 'Admin'), ('client', 'Client')], db_index=True, max_length=10)),
                ('sender_name', models.CharField(blank=True, max_length=150)),
                ('body', models.TextField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('read_by_admin_at', models.DateTimeField(blank=True, null=True)),
                ('read_by_client_at', models.DateTimeField(blank=True, null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='messages', to='projects.project')),
                ('sender_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='project_messages', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='project',
            index=models.Index(fields=['owner', 'status'], name='projects_pr_owner_i_5f1b8a_idx'),
        ),
        migrations.AddIndex(
            model_name='project',
            index=models.Index(fields=['status', 'created_at'], name='projects_pr_status_9c2d4e_idx'),
        ),
        migrations.AddIndex(
            model_name='projectmessage',
            index=models.Index(fields=['project', 'sender_type', 'read_by_admin_at'], name='projects_pm_admin_unread_idx'),
        ),
        migrations.AddIndex(
            model_name='projectmessage',
            index=models.Index(fields=['project', 'sender_type', 'read_by_client_at'], name='projects_pm_client_unread_idx'),
        ),
    ]
