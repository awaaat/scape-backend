from django.db import migrations, models
import django.db.models.deletion

import jobs.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('visitors', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='JobPosting',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('slug', models.SlugField(blank=True, max_length=220, unique=True)),
                ('department', models.CharField(choices=[('engineering', 'Engineering'), ('data', 'Data & Analytics'), ('ai_ml', 'AI & Machine Learning'), ('sales', 'Sales'), ('marketing', 'Marketing'), ('customer_success', 'Customer Success'), ('operations', 'Operations'), ('finance', 'Finance'), ('hr', 'People & HR'), ('other', 'Other')], default='other', max_length=30)),
                ('job_type', models.CharField(choices=[('full_time', 'Full-time'), ('part_time', 'Part-time'), ('contract', 'Contract'), ('internship', 'Internship'), ('freelance', 'Freelance')], default='full_time', max_length=20)),
                ('location_type', models.CharField(choices=[('remote', 'Remote'), ('onsite', 'On-site'), ('hybrid', 'Hybrid')], default='remote', max_length=10)),
                ('location', models.CharField(blank=True, help_text="e.g. 'Nairobi, Kenya' or 'Worldwide'", max_length=150)),
                ('experience_level', models.CharField(choices=[('entry', 'Entry Level'), ('mid', 'Mid Level'), ('senior', 'Senior'), ('lead', 'Lead / Principal'), ('executive', 'Executive')], default='mid', max_length=15)),
                ('summary', models.CharField(blank=True, help_text='One-line summary shown on the jobs list page', max_length=300)),
                ('description', models.TextField(help_text='Full role description')),
                ('responsibilities', models.TextField(blank=True, help_text='One item per line — rendered as a bullet list on the site')),
                ('requirements', models.TextField(blank=True, help_text='One item per line — rendered as a bullet list on the site')),
                ('nice_to_have', models.TextField(blank=True, help_text='One item per line — rendered as a bullet list on the site')),
                ('salary_range', models.CharField(blank=True, help_text="e.g. '$60,000 - $80,000 / year'. Leave blank to hide salary entirely.", max_length=100)),
                ('positions_available', models.PositiveSmallIntegerField(default=1)),
                ('is_active', models.BooleanField(db_index=True, default=True, help_text='Untick to hide this posting without deleting it')),
                ('is_featured', models.BooleanField(default=False, help_text='Featured jobs are pinned to the top of the public list')),
                ('posted_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('closing_date', models.DateField(blank=True, help_text='Optional — applications stop being accepted after this date', null=True)),
            ],
            options={
                'ordering': ['-is_featured', '-posted_at'],
            },
        ),
        migrations.CreateModel(
            name='JobApplication',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('full_name', models.CharField(max_length=150)),
                ('email', models.EmailField(max_length=254)),
                ('phone', models.CharField(blank=True, max_length=30)),
                ('linkedin_url', models.URLField(blank=True)),
                ('portfolio_url', models.URLField(blank=True)),
                ('current_company', models.CharField(blank=True, max_length=150)),
                ('years_of_experience', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('expected_salary', models.CharField(blank=True, max_length=100)),
                ('resume', models.FileField(upload_to=jobs.models.resume_upload_path, validators=[jobs.models.validate_resume_file])),
                ('cover_letter', models.TextField(blank=True)),
                ('how_heard', models.CharField(blank=True, help_text='How did they hear about this role?', max_length=150)),
                ('status', models.CharField(choices=[('new', 'New'), ('reviewing', 'Reviewing'), ('shortlisted', 'Shortlisted'), ('interviewing', 'Interviewing'), ('offer_extended', 'Offer Extended'), ('hired', 'Hired'), ('rejected', 'Rejected'), ('withdrawn', 'Withdrawn')], db_index=True, default='new', max_length=20)),
                ('internal_notes', models.TextField(blank=True, help_text='Not visible to the applicant')),
                ('confirmation_email_sent', models.BooleanField(default=False)),
                ('admin_notified', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='applications', to='jobs.jobposting')),
                ('visitor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='job_applications', to='visitors.visitor')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='jobapplication',
            constraint=models.UniqueConstraint(fields=('job', 'email'), name='unique_application_per_job_email'),
        ),
    ]
