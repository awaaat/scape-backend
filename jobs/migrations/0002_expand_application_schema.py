

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('jobs', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='jobapplication',
            name='resume',
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='city',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='consent_given',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='consent_given_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='country',
            field=models.CharField(blank=True, choices=[('AF', 'Afghanistan'), ('AL', 'Albania'), ('DZ', 'Algeria'), ('AS', 'American Samoa'), ('AD', 'Andorra'), ('AO', 'Angola'), ('AI', 'Anguilla'), ('AQ', 'Antarctica'), ('AG', 'Antigua and Barbuda'), ('AR', 'Argentina'), ('AM', 'Armenia'), ('AW', 'Aruba'), ('AU', 'Australia'), ('AT', 'Austria'), ('AZ', 'Azerbaijan'), ('BS', 'Bahamas'), ('BH', 'Bahrain'), ('BD', 'Bangladesh'), ('BB', 'Barbados'), ('BY', 'Belarus'), ('BE', 'Belgium'), ('BZ', 'Belize'), ('BJ', 'Benin'), ('BM', 'Bermuda'), ('BT', 'Bhutan'), ('BO', 'Bolivia, Plurinational State of'), ('BQ', 'Bonaire, Sint Eustatius and Saba'), ('BA', 'Bosnia and Herzegovina'), ('BW', 'Botswana'), ('BV', 'Bouvet Island'), ('BR', 'Brazil'), ('IO', 'British Indian Ocean Territory'), ('BN', 'Brunei Darussalam'), ('BG', 'Bulgaria'), ('BF', 'Burkina Faso'), ('BI', 'Burundi'), ('CV', 'Cabo Verde'), ('KH', 'Cambodia'), ('CM', 'Cameroon'), ('CA', 'Canada'), ('KY', 'Cayman Islands'), ('CF', 'Central African Republic'), ('TD', 'Chad'), ('CL', 'Chile'), ('CN', 'China'), ('CX', 'Christmas Island'), ('CC', 'Cocos (Keeling) Islands'), ('CO', 'Colombia'), ('KM', 'Comoros'), ('CG', 'Congo'), ('CD', 'Congo, The Democratic Republic of the'), ('CK', 'Cook Islands'), ('CR', 'Costa Rica'), ('HR', 'Croatia'), ('CU', 'Cuba'), ('CW', 'Curaçao'), ('CY', 'Cyprus'), ('CZ', 'Czechia'), ('CI', "Côte d'Ivoire"), ('DK', 'Denmark'), ('DJ', 'Djibouti'), ('DM', 'Dominica'), ('DO', 'Dominican Republic'), ('EC', 'Ecuador'), ('EG', 'Egypt'), ('SV', 'El Salvador'), ('GQ', 'Equatorial Guinea'), ('ER', 'Eritrea'), ('EE', 'Estonia'), ('SZ', 'Eswatini'), ('ET', 'Ethiopia'), ('FK', 'Falkland Islands (Malvinas)'), ('FO', 'Faroe Islands'), ('FJ', 'Fiji'), ('FI', 'Finland'), ('FR', 'France'), ('GF', 'French Guiana'), ('PF', 'French Polynesia'), ('TF', 'French Southern Territories'), ('GA', 'Gabon'), ('GM', 'Gambia'), ('GE', 'Georgia'), ('DE', 'Germany'), ('GH', 'Ghana'), ('GI', 'Gibraltar'), ('GR', 'Greece'), ('GL', 'Greenland'), ('GD', 'Grenada'), ('GP', 'Guadeloupe'), ('GU', 'Guam'), ('GT', 'Guatemala'), ('GG', 'Guernsey'), ('GN', 'Guinea'), ('GW', 'Guinea-Bissau'), ('GY', 'Guyana'), ('HT', 'Haiti'), ('HM', 'Heard Island and McDonald Islands'), ('VA', 'Holy See (Vatican City State)'), ('HN', 'Honduras'), ('HK', 'Hong Kong'), ('HU', 'Hungary'), ('IS', 'Iceland'), ('IN', 'India'), ('ID', 'Indonesia'), ('IR', 'Iran, Islamic Republic of'), ('IQ', 'Iraq'), ('IE', 'Ireland'), ('IM', 'Isle of Man'), ('IL', 'Israel'), ('IT', 'Italy'), ('JM', 'Jamaica'), ('JP', 'Japan'), ('JE', 'Jersey'), ('JO', 'Jordan'), ('KZ', 'Kazakhstan'), ('KE', 'Kenya'), ('KI', 'Kiribati'), ('KP', "Korea, Democratic People's Republic of"), ('KR', 'Korea, Republic of'), ('KW', 'Kuwait'), ('KG', 'Kyrgyzstan'), ('LA', "Lao People's Democratic Republic"), ('LV', 'Latvia'), ('LB', 'Lebanon'), ('LS', 'Lesotho'), ('LR', 'Liberia'), ('LY', 'Libya'), ('LI', 'Liechtenstein'), ('LT', 'Lithuania'), ('LU', 'Luxembourg'), ('MO', 'Macao'), ('MG', 'Madagascar'), ('MW', 'Malawi'), ('MY', 'Malaysia'), ('MV', 'Maldives'), ('ML', 'Mali'), ('MT', 'Malta'), ('MH', 'Marshall Islands'), ('MQ', 'Martinique'), ('MR', 'Mauritania'), ('MU', 'Mauritius'), ('YT', 'Mayotte'), ('MX', 'Mexico'), ('FM', 'Micronesia, Federated States of'), ('MD', 'Moldova, Republic of'), ('MC', 'Monaco'), ('MN', 'Mongolia'), ('ME', 'Montenegro'), ('MS', 'Montserrat'), ('MA', 'Morocco'), ('MZ', 'Mozambique'), ('MM', 'Myanmar'), ('NA', 'Namibia'), ('NR', 'Nauru'), ('NP', 'Nepal'), ('NL', 'Netherlands'), ('NC', 'New Caledonia'), ('NZ', 'New Zealand'), ('NI', 'Nicaragua'), ('NE', 'Niger'), ('NG', 'Nigeria'), ('NU', 'Niue'), ('NF', 'Norfolk Island'), ('MK', 'North Macedonia'), ('MP', 'Northern Mariana Islands'), ('NO', 'Norway'), ('OM', 'Oman'), ('PK', 'Pakistan'), ('PW', 'Palau'), ('PS', 'Palestine, State of'), ('PA', 'Panama'), ('PG', 'Papua New Guinea'), ('PY', 'Paraguay'), ('PE', 'Peru'), ('PH', 'Philippines'), ('PN', 'Pitcairn'), ('PL', 'Poland'), ('PT', 'Portugal'), ('PR', 'Puerto Rico'), ('QA', 'Qatar'), ('RO', 'Romania'), ('RU', 'Russian Federation'), ('RW', 'Rwanda'), ('RE', 'Réunion'), ('BL', 'Saint Barthélemy'), ('SH', 'Saint Helena, Ascension and Tristan da Cunha'), ('KN', 'Saint Kitts and Nevis'), ('LC', 'Saint Lucia'), ('MF', 'Saint Martin (French part)'), ('PM', 'Saint Pierre and Miquelon'), ('VC', 'Saint Vincent and the Grenadines'), ('WS', 'Samoa'), ('SM', 'San Marino'), ('ST', 'Sao Tome and Principe'), ('SA', 'Saudi Arabia'), ('SN', 'Senegal'), ('RS', 'Serbia'), ('SC', 'Seychelles'), ('SL', 'Sierra Leone'), ('SG', 'Singapore'), ('SX', 'Sint Maarten (Dutch part)'), ('SK', 'Slovakia'), ('SI', 'Slovenia'), ('SB', 'Solomon Islands'), ('SO', 'Somalia'), ('ZA', 'South Africa'), ('GS', 'South Georgia and the South Sandwich Islands'), ('SS', 'South Sudan'), ('ES', 'Spain'), ('LK', 'Sri Lanka'), ('SD', 'Sudan'), ('SR', 'Suriname'), ('SJ', 'Svalbard and Jan Mayen'), ('SE', 'Sweden'), ('CH', 'Switzerland'), ('SY', 'Syrian Arab Republic'), ('TW', 'Taiwan, Province of China'), ('TJ', 'Tajikistan'), ('TZ', 'Tanzania, United Republic of'), ('TH', 'Thailand'), ('TL', 'Timor-Leste'), ('TG', 'Togo'), ('TK', 'Tokelau'), ('TO', 'Tonga'), ('TT', 'Trinidad and Tobago'), ('TN', 'Tunisia'), ('TM', 'Turkmenistan'), ('TC', 'Turks and Caicos Islands'), ('TV', 'Tuvalu'), ('TR', 'Türkiye'), ('UG', 'Uganda'), ('UA', 'Ukraine'), ('AE', 'United Arab Emirates'), ('GB', 'United Kingdom'), ('US', 'United States'), ('UM', 'United States Minor Outlying Islands'), ('UY', 'Uruguay'), ('UZ', 'Uzbekistan'), ('VU', 'Vanuatu'), ('VE', 'Venezuela, Bolivarian Republic of'), ('VN', 'Viet Nam'), ('VG', 'Virgin Islands, British'), ('VI', 'Virgin Islands, U.S.'), ('WF', 'Wallis and Futuna'), ('EH', 'Western Sahara'), ('YE', 'Yemen'), ('ZM', 'Zambia'), ('ZW', 'Zimbabwe'), ('AX', 'Åland Islands')], max_length=2),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='date_of_birth',
            field=models.DateField(blank=True, help_text='Collected on request. Keep access to this field restricted — most jurisdictions advise against hiring managers seeing DOB pre-decision.', null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='earliest_start_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='github_url',
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='ip_address',
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='notice_period',
            field=models.CharField(blank=True, choices=[('immediate', 'Immediately available'), ('1_week', '1 week'), ('2_weeks', '2 weeks'), ('1_month', '1 month'), ('2_months', '2 months'), ('3_months_plus', '3+ months')], max_length=20),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='open_to_relocation',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='parsed_resume',
            field=models.JSONField(blank=True, default=dict, help_text='Best-effort structured data extracted from the resume. See resume_parser.py.'),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='postal_code',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='privacy_policy_version',
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='remote_preference',
            field=models.CharField(blank=True, choices=[('remote', 'Remote'), ('hybrid', 'Hybrid'), ('onsite', 'On-site'), ('no_preference', 'No preference')], max_length=20),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='resume_filename',
            field=models.CharField(blank=True, help_text='Original filename, kept for reference only — no file is stored.', max_length=255),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='resume_parse_failed',
            field=models.BooleanField(default=False, help_text="True if the uploaded file couldn't be parsed (e.g. scanned image PDF)."),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='resume_raw_text',
            field=models.TextField(blank=True, help_text='Plain text extracted from the uploaded resume (truncated).'),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='stackoverflow_url',
            field=models.URLField(blank=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='user_agent',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='visa_sponsorship_required',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='jobapplication',
            name='work_authorization',
            field=models.CharField(blank=True, choices=[('citizen', 'Citizen of country applying from'), ('permanent_resident', 'Permanent resident / holds equivalent right to work'), ('visa_holder', 'Currently holds a valid work visa'), ('needs_sponsorship', 'Will require visa sponsorship'), ('other', 'Other')], max_length=20),
        ),
        migrations.AlterField(
            model_name='jobapplication',
            name='portfolio_url',
            field=models.URLField(blank=True, help_text='Personal site, Behance, Dribbble, Kaggle, etc.'),
        ),
        migrations.CreateModel(
            name='JobApplicationDemographics',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('gender', models.CharField(blank=True, choices=[('female', 'Female'), ('male', 'Male'), ('non_binary', 'Non-binary'), ('self_describe', 'Prefer to self-describe'), ('prefer_not_to_say', 'Prefer not to say')], max_length=20)),
                ('self_described_gender', models.CharField(blank=True, max_length=100)),
                ('veteran_status', models.CharField(blank=True, choices=[('veteran', 'Yes, I am a veteran / have served'), ('not_veteran', 'No'), ('prefer_not_to_say', 'Prefer not to say')], max_length=20)),
                ('disability_status', models.CharField(blank=True, choices=[('yes', 'Yes, I have a disability (or have had one)'), ('no', 'No'), ('prefer_not_to_say', 'Prefer not to say')], max_length=20)),
                ('ethnicity', models.CharField(blank=True, max_length=100)),
                ('collected_at', models.DateTimeField(auto_now_add=True)),
                ('application', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='demographics', to='jobs.jobapplication')),
            ],
            options={
                'verbose_name': 'demographics (EEO, optional)',
                'verbose_name_plural': 'demographics (EEO, optional)',
            },
        ),
        migrations.CreateModel(
            name='EmploymentEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('company', models.CharField(blank=True, max_length=200)),
                ('job_title', models.CharField(blank=True, max_length=200)),
                ('start_date', models.DateField(blank=True, null=True)),
                ('end_date', models.DateField(blank=True, null=True)),
                ('is_current', models.BooleanField(default=False)),
                ('responsibilities', models.TextField(blank=True)),
                ('auto_extracted', models.BooleanField(default=False, help_text='True if this row came from resume parsing rather than manual entry.')),
                ('order', models.PositiveSmallIntegerField(default=0)),
                ('application', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='employment_history', to='jobs.jobapplication')),
            ],
            options={
                'verbose_name_plural': 'employment entries',
                'ordering': ['order', '-start_date'],
            },
        ),
        migrations.CreateModel(
            name='EducationEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('school', models.CharField(blank=True, max_length=200)),
                ('degree', models.CharField(blank=True, max_length=200)),
                ('field_of_study', models.CharField(blank=True, max_length=200)),
                ('graduation_year', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('gpa', models.CharField(blank=True, max_length=20)),
                ('auto_extracted', models.BooleanField(default=False, help_text='True if this row came from resume parsing rather than manual entry.')),
                ('order', models.PositiveSmallIntegerField(default=0)),
                ('application', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='education_history', to='jobs.jobapplication')),
            ],
            options={
                'verbose_name_plural': 'education entries',
                'ordering': ['order', '-graduation_year'],
            },
        ),
    ]
