"""
orders/catalog.py — the SITE REGISTRY.

Every front end that takes orders (parent site, thesis site, anything you add
later) is one entry in SITES below. A site owns: its services, its questions
and their ORDER, its option lists, who gets notified, and where its "finish
your order" resume link points. The backend tells each front end what to ask:
GET /api/orders/catalog/ returns the config for whichever site is calling.

HOW A REQUEST IS MATCHED TO A SITE (first hit wins)
  1. explicit  `?site=thesis` (catalog) / `"site": "thesis"` (start, quick)
  2. the browser's Origin / Referer host  -> the site whose `hosts` contains it
  3. DEFAULT_SITE
Local dev (localhost) has no matching host, so pass the explicit key there.

ADD A NEW SITE
  1. Add one build_site(...) block below and list it in SITES.
  2. Deploy. That's all — CORS for the site's `hosts` is opened automatically
     for /api/orders/ (see apps.py), even on a completely different domain.
     (Other APIs the new site calls, e.g. /api/jobs/, still need its origin in
     the FRONTEND_ORIGINS env var unless it's a *.scapedatasolutions.com host.)

FIELDS
  Core fields live in real columns (CORE_FIELDS). ANY other field name used in a
  step's `fields` list is a custom field: the front end just sends it like any
  other ({"budget": "..."}) and the backend files it under `answers`, shows it
  in the emails/admin with the label from `labels`, and keeps it queryable.
"""
from urllib.parse import urlparse

from django.conf import settings

DEFAULT_SITE = "main"

CORE_FIELDS = {
    "name", "email", "phone", "country", "service", "academic_level", "data_status",
    "software", "deadline_window", "deadline", "topic", "details",
}

ALLOWED_EXTENSIONS = {
    "sav", "zsav", "por", "csv", "tsv", "xlsx", "xls", "xlsm", "dta", "rdata", "rds", "sas7bdat",
    "omv", "jasp", "zip", "pdf", "doc", "docx", "txt", "rtf", "pptx", "json", "png", "jpg", "jpeg",
}

OTHER_SERVICE = {
    "slug": "other", "label": "Something else", "category": "other",
    "blurb": "Not listed? Tell us what you need.",
}

_CONTACT_STEP = {
    "key": "contact", "title": "Where should we send your quote?",
    "subtitle": "We reply within 24 hours, usually sooner. Your details stay confidential.",
    "input": "form", "fields": ["name", "email", "phone", "country"], "required": ["name", "email"],
}
_REVIEW_STEP = {
    "key": "review", "title": "Review and place your order",
    "subtitle": "You pay nothing until you agree to our quote.",
    "input": "review", "fields": [], "required": [],
}


def _auto_label(key):
    return key.replace("_", " ").capitalize()


def build_site(key, *, name, hosts, frontend_url, services, steps, options=None, categories=None,
               labels=None, required=("name", "email", "service"), notify_emails=None,
               resume_path="/order-now", allow_other=True, promise=None):
    """Fills in defaults so a new site is just its services + its steps."""
    answer_fields = []
    for st in steps:
        for f in st.get("fields", []):
            if f not in CORE_FIELDS and f not in answer_fields:
                answer_fields.append(f)
    return {
        "key": key, "name": name,
        "hosts": {h.lower() for h in hosts},
        "frontend_url": frontend_url.rstrip("/"), "resume_path": resume_path,
        "notify_emails": notify_emails,          # None = use ORDER_NOTIFICATION_EMAILS
        "categories": categories or [],
        "services": services, "allow_other": allow_other,
        "options": options or {}, "steps": steps,
        "labels": labels or {}, "required": tuple(required),
        "answer_fields": set(answer_fields),
        "promise": promise or {"reply_within": "24 hours", "pay_nothing_until_you_agree": True},
    }


# ═════════════════════════════════════════════════════════════════════
#  SITE: thesis.scapedatasolutions.com  — dissertation / data-analysis help
# ═════════════════════════════════════════════════════════════════════
THESIS = build_site(
    "thesis", name="Scape Thesis Data Analysis",
    hosts=["thesis.scapedatasolutions.com"],
    frontend_url="https://thesis.scapedatasolutions.com",
    categories=[
        {"id": "core", "title": "Thesis and core analysis"},
        {"id": "prep", "title": "Data preparation"},
        {"id": "scales", "title": "Questionnaires and scales"},
        {"id": "advanced", "title": "Advanced models"},
    ],
    services=[
        {"slug": "spss-data-analysis-help", "label": "SPSS Data Analysis Help", "category": "core",
         "blurb": "The right test for each objective, APA tables, syntax and interpretation."},
        {"slug": "thesis-data-analysis-help", "label": "Thesis Data Analysis Help", "category": "core",
         "blurb": "Full Chapter 4 analysis for your thesis or dissertation."},
        {"slug": "excel-statistical-analysis-help", "label": "Excel Statistical Analysis", "category": "core",
         "blurb": "Descriptives, tests and charts done properly in Excel."},
        {"slug": "research-results-interpretation", "label": "Research Results Interpretation", "category": "core",
         "blurb": "Plain-English interpretation of output you already have."},
        {"slug": "regression-analysis-help", "label": "Regression Analysis Help", "category": "core",
         "blurb": "Linear, logistic, multiple and hierarchical regression."},
        {"slug": "chapter-4-results-writing-help", "label": "Chapter 4 Results Writing", "category": "core",
         "blurb": "Turn your output into a polished results chapter."},
        {"slug": "research-data-cleaning-service", "label": "Research Data Cleaning", "category": "prep",
         "blurb": "Missing values, outliers, coding and a dataset ready to analyse."},
        {"slug": "kobotoolbox-odk-data-analysis-help", "label": "KoboToolbox and ODK Data", "category": "prep",
         "blurb": "Export, clean and analyse field-survey data."},
        {"slug": "questionnaire-data-analysis-help", "label": "Questionnaire Data Analysis", "category": "scales",
         "blurb": "From raw responses to tested hypotheses."},
        {"slug": "likert-scale-data-analysis", "label": "Likert Scale Data Analysis", "category": "scales",
         "blurb": "Correct handling and testing of Likert items and scales."},
        {"slug": "cronbach-alpha-reliability-analysis", "label": "Cronbach's Alpha Reliability", "category": "scales",
         "blurb": "Reliability testing and what to do when alpha is low."},
        {"slug": "factor-analysis-spss-help", "label": "Factor Analysis (EFA and CFA)", "category": "scales",
         "blurb": "Exploratory and confirmatory factor analysis."},
        {"slug": "mediation-moderation-analysis-help", "label": "Mediation and Moderation", "category": "advanced",
         "blurb": "PROCESS-style mediation and moderation models."},
        {"slug": "sem-amos-smartpls-help", "label": "SEM: AMOS and SmartPLS", "category": "advanced",
         "blurb": "Structural equation models, fit indices and reporting."},
        {"slug": "sample-size-calculation-help", "label": "Sample Size and Power Analysis", "category": "advanced",
         "blurb": "Defensible sample size with the working shown."},
    ],
    options={
        "academic_level": ["Undergraduate", "Masters", "PhD", "Journal paper / research", "Other"],
        "data_status": ["I have my dataset ready", "Data collected, needs cleaning",
                        "I only have output or results", "I haven't collected data yet", "Not sure"],
        "software": ["SPSS", "Excel", "R", "Stata", "Python", "AMOS / SmartPLS", "Not sure"],
        "deadline_window": ["Within 48 hours", "Within a week", "1-2 weeks", "2-4 weeks",
                            "More than a month", "Flexible / not sure"],
    },
    labels={"academic_level": "Academic level", "data_status": "Data status"},
    steps=[
        {"key": "service", "title": "What do you need help with?",
         "subtitle": "Pick the closest match. You can add detail in a moment.",
         "input": "cards", "fields": ["service"], "required": ["service"]},
        _CONTACT_STEP,
        {"key": "level", "title": "What is your academic level?",
         "input": "cards", "fields": ["academic_level"], "options_from": "academic_level", "required": []},
        {"key": "data", "title": "Where are you with your data?",
         "input": "cards", "fields": ["data_status"], "options_from": "data_status", "required": []},
        {"key": "software", "title": "Which software do you use or need?",
         "input": "cards", "fields": ["software"], "options_from": "software", "required": []},
        {"key": "deadline", "title": "When do you need it?",
         "subtitle": "Tight deadline? We prioritise urgent orders.",
         "input": "cards+date", "fields": ["deadline_window", "deadline"],
         "options_from": "deadline_window", "required": []},
        {"key": "details", "title": "Tell us about your project",
         "subtitle": "Objectives, sample size, supervisor comments. Upload your dataset if you have it.",
         "input": "textarea+files", "fields": ["topic", "details"], "required": []},
        _REVIEW_STEP,
    ],
)

# ═════════════════════════════════════════════════════════════════════
#  SITE: www.scapedatasolutions.com  — parent company site
#  !! PLACEHOLDER services/steps — I only had the thesis front end, so this
#  !! is a sensible starting point built from what the backend does. Edit
#  !! the services list to your real offerings (or send them to me).
# ═════════════════════════════════════════════════════════════════════
MAIN = build_site(
    "main", name="Scape Data Solutions",
    hosts=["www.scapedatasolutions.com", "scapedatasolutions.com"],
    frontend_url="https://www.scapedatasolutions.com",
    categories=[
        {"id": "intel", "title": "Intelligence reports"},
        {"id": "data", "title": "Data and analytics"},
        {"id": "build", "title": "Build and automate"},
    ],
    services=[
        {"slug": "business-location-intelligence", "label": "Business Location Intelligence", "category": "intel",
         "blurb": "Where to open, who your competitors are, what the market can bear."},
        {"slug": "property-location-report", "label": "Property Location Report", "category": "intel",
         "blurb": "Verified amenities, access and risk around a property."},
        {"slug": "data-analysis-statistics", "label": "Data Analysis and Statistics", "category": "data",
         "blurb": "Statistical analysis and clear, defensible reporting."},
        {"slug": "dashboards-reporting", "label": "Dashboards and Reporting", "category": "data",
         "blurb": "Live dashboards and recurring reports your team will use."},
        {"slug": "data-engineering-automation", "label": "Data Engineering and Automation", "category": "build",
         "blurb": "Pipelines, integrations and workflow automation."},
        {"slug": "custom-data-project", "label": "Custom Data Project", "category": "build",
         "blurb": "Bespoke scope. Tell us the outcome you need."},
    ],
    options={
        "deadline_window": ["As soon as possible", "Within 2 weeks", "Within a month",
                            "1-3 months", "Flexible"],
        "budget": ["Under $500", "$500 - $2,000", "$2,000 - $10,000", "$10,000+", "Not sure yet"],
        "company_size": ["Just me", "2-10", "11-50", "51-200", "200+"],
    },
    labels={"budget": "Budget range", "company_size": "Company size", "company": "Company"},
    steps=[
        {"key": "service", "title": "What do you need?",
         "subtitle": "Pick the closest match.", "input": "cards", "fields": ["service"], "required": ["service"]},
        _CONTACT_STEP,
        {"key": "company", "title": "Tell us about your organisation",
         "input": "form", "fields": ["company", "company_size"], "options_from": "company_size", "required": []},
        {"key": "timeline", "title": "When do you need this?",
         "input": "cards+date", "fields": ["deadline_window", "deadline"],
         "options_from": "deadline_window", "required": []},
        {"key": "budget", "title": "What's your budget?",
         "subtitle": "A rough range helps us scope the right solution.",
         "input": "cards", "fields": ["budget"], "options_from": "budget", "required": []},
        {"key": "details", "title": "Tell us about the project",
         "subtitle": "The problem, the outcome you want, and anything you already have.",
         "input": "textarea+files", "fields": ["topic", "details"], "required": []},
        _REVIEW_STEP,
    ],
)

SITES = {s["key"]: s for s in (THESIS, MAIN)}


# ── lookups ──────────────────────────────────────────────────────────
def get_site(key):
    """Never raises: unknown/blank keys fall back to the default site."""
    return SITES.get(key) or SITES[DEFAULT_SITE]


def _host(value):
    return (urlparse(value or "").hostname or "").lower()


def site_for_origin(url):
    host = _host(url)
    if not host:
        return None
    for s in SITES.values():
        if host in s["hosts"]:
            return s
    return None


def resolve_site(request=None, explicit=None):
    if explicit and explicit in SITES:
        return SITES[explicit]
    if request is not None:
        q = getattr(request, "query_params", None)
        qs = q.get("site") if q is not None else None
        if qs and qs in SITES:
            return SITES[qs]
        for header in ("HTTP_ORIGIN", "HTTP_REFERER"):
            s = site_for_origin(request.META.get(header, ""))
            if s:
                return s
    return SITES[DEFAULT_SITE]


def field_label(site_key, key):
    return get_site(site_key)["labels"].get(key) or _auto_label(key)


def resolve_service(site, value):
    """Accepts a slug OR a label and returns (slug, label). Never rejects:
    unknown text becomes 'Other: ...'."""
    v = (value or "").strip()
    low = v.lower()
    for s in site["services"]:
        if low in (s["slug"], s["label"].lower()):
            return s["slug"], s["label"]
    if low in ("other", "something else", ""):
        return OTHER_SERVICE["slug"], OTHER_SERVICE["label"]
    return OTHER_SERVICE["slug"], f"Other: {v}"[:150]


def public_catalog(site):
    return {
        "version": 2,
        "site": {"key": site["key"], "name": site["name"]},
        "promise": site["promise"],
        "categories": site["categories"],
        "services": site["services"] + ([OTHER_SERVICE] if site["allow_other"] else []),
        "options": site["options"],
        "labels": site["labels"],
        "steps": site["steps"],
        "required_to_submit": list(site["required"]),
        "limits": {
            "max_files": getattr(settings, "ORDER_MAX_FILES", 10),
            "max_file_mb": getattr(settings, "ORDER_FILE_MAX_MB", 25),
            "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        },
    }
