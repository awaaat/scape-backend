from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('contracts', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ContractChangeRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requested_by_name', models.CharField(blank=True, max_length=150)),
                ('proposed_title', models.CharField(blank=True, max_length=255)),
                ('proposed_scope_of_work', models.TextField(blank=True)),
                ('proposed_total_value', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('proposed_currency', models.CharField(blank=True, max_length=3)),
                ('reason', models.TextField(help_text="Client's explanation for the requested change.")),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('approved', 'Approved'), ('rejected', 'Rejected'), ('withdrawn', 'Withdrawn')], db_index=True, default='pending', max_length=20)),
                ('admin_note', models.TextField(blank=True, help_text='Set by the admin when approving or rejecting.')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('contract', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='change_requests', to='contracts.contract')),
                ('requested_by_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contract_change_requests', to=settings.AUTH_USER_MODEL)),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contract_change_requests_reviewed', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ContractDispute',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('raised_by_name', models.CharField(blank=True, max_length=150)),
                ('category', models.CharField(choices=[('deliverable_not_as_described', 'Deliverable not as described'), ('missed_deadline', 'Missed deadline'), ('payment_issue', 'Payment issue'), ('communication_issue', 'Communication issue'), ('other', 'Other')], default='other', max_length=40)),
                ('description', models.TextField()),
                ('status', models.CharField(choices=[('open', 'Open'), ('under_review', 'Under review'), ('resolved_client_favor', 'Resolved — client favor'), ('resolved_provider_favor', 'Resolved — provider favor'), ('resolved_mutual', 'Resolved — mutual agreement'), ('withdrawn', 'Withdrawn')], db_index=True, default='open', max_length=30)),
                ('resolution_note', models.TextField(blank=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('contract', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='disputes', to='contracts.contract')),
                ('raised_by_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contract_disputes', to=settings.AUTH_USER_MODEL)),
                ('resolved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='contract_disputes_resolved', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='contractdispute',
            index=models.Index(fields=['status', 'created_at'], name='contracts_cd_status_created_idx'),
        ),
    ]
