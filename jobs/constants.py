"""
Static choice lists shared across models/serializers/admin.

COUNTRY_CHOICES is generated from `pycountry` (the standard ISO-3166-1
country database) at import time, so it's always a complete, correctly
coded list — no hand-maintained country list to go stale.
"""
import pycountry

COUNTRY_CHOICES = sorted(
    [(country.alpha_2, country.name) for country in pycountry.countries],
    key=lambda c: c[1],
)

WORK_AUTH_CHOICES = [
    ("citizen", "Citizen of country applying from"),
    ("permanent_resident", "Permanent resident / holds equivalent right to work"),
    ("visa_holder", "Currently holds a valid work visa"),
    ("needs_sponsorship", "Will require visa sponsorship"),
    ("other", "Other"),
]

NOTICE_PERIOD_CHOICES = [
    ("immediate", "Immediately available"),
    ("1_week", "1 week"),
    ("2_weeks", "2 weeks"),
    ("1_month", "1 month"),
    ("2_months", "2 months"),
    ("3_months_plus", "3+ months"),
]

GENDER_CHOICES = [
    ("female", "Female"),
    ("male", "Male"),
    ("non_binary", "Non-binary"),
    ("self_describe", "Prefer to self-describe"),
    ("prefer_not_to_say", "Prefer not to say"),
]

VETERAN_STATUS_CHOICES = [
    ("veteran", "Yes, I am a veteran / have served"),
    ("not_veteran", "No"),
    ("prefer_not_to_say", "Prefer not to say"),
]

DISABILITY_STATUS_CHOICES = [
    ("yes", "Yes, I have a disability (or have had one)"),
    ("no", "No"),
    ("prefer_not_to_say", "Prefer not to say"),
]

REMOTE_PREFERENCE_CHOICES = [
    ("remote", "Remote"),
    ("hybrid", "Hybrid"),
    ("onsite", "On-site"),
    ("no_preference", "No preference"),
]
