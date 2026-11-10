"""
orders/engagement_intel.py

The "brain" of the engagement layer. Pure functions where possible, so every
decision is explainable and unit-testable.

  conf()            - every tunable, read from Django settings on each call
                      (so override_settings works and Render env changes apply).
  score_order()     - 0-100 lead score + HOT/WARM/COLD tier + a human-readable
                      breakdown ("why is this lead hot?").
  infer_timezone()  - best guess of the client's local timezone from the
                      country they typed, their phone dial code, their IP.
  next_send_time()  - pushes a due time out of the client's quiet hours.
  ladder_for()      - the reminder schedule for THIS order: urgent when the
                      deadline is close, stretched for cold leads, tightened
                      for hot ones, overridable per site.
  journey_summary() - one line for the admin emails.
"""
import re
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone
from django.utils.timesince import timesince

try:  # zoneinfo is stdlib on 3.9+, but the tz database may be missing on slim images
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass


# ═════════════════════════════════════════════════════════════════════
#  Settings
# ═════════════════════════════════════════════════════════════════════
_DEFAULTS = {
    # master switches
    "ORDER_ENGAGEMENT_ENABLED": True,
    "ORDER_INTENT_ALERTS_ENABLED": True,
    "ORDER_REMINDERS_ENABLED": True,
    # only manage orders created at/after this moment (ISO-8601). "" = no limit.
    "ORDER_ENGAGEMENT_SINCE": "",
    # intent alert
    "ORDER_INTENT_GRACE_SECONDS": 90,     # sweeper picks up leads the hook missed after this idle time
    "ORDER_INTENT_DEDUPE_HOURS": 6,       # same email re-starting within this window = one alert
    # exit detection
    "ORDER_EXIT_GRACE_MINUTES": 5,        # beacon said "left"; wait this long for them to come back
    # reminder ladder (hours of silence since last activity)
    "ORDER_REMINDER_LADDER_HOURS": [24, 72, 168, 336],
    "ORDER_REMINDER_URGENT_LADDER_HOURS": [4, 24, 48],
    "ORDER_URGENT_DAYS": 3,
    "ORDER_REMINDER_MAX_LIFETIME": 6,     # hard cap per order, across ladder resets
    "ORDER_REMINDER_MAX_PER_EMAIL_WEEK": 3,
    "ORDER_REMINDER_MIN_GAP_HOURS": 12,
    "ORDER_REMINDER_MAX_AGE_DAYS": 30,
    "ORDER_REMINDER_WINDOW_START_HOUR": 8,
    "ORDER_REMINDER_WINDOW_END_HOUR": 20,
    "ORDER_REMINDER_DEFAULT_TZ": "Africa/Nairobi",
    # scoring
    "ORDER_HOT_SCORE": 65,
    "ORDER_WARM_SCORE": 40,
    "ORDER_HOT_FOLLOWUP_HOURS": 24,
    "ORDER_CLICK_FOLLOWUP_HOURS": 2,
    "ORDER_CLICK_MIN_SECONDS": 8,         # clicks faster than this after send = mail scanner, not a human
    # links + branding
    "ORDERS_API_BASE_URL": "",
    "ORDER_BUSINESS_WHATSAPP": "",
    # digest
    "ORDER_DIGEST_ENABLED": True,
}


def conf(name):
    return getattr(settings, name, _DEFAULTS[name])


def engagement_enabled():
    return bool(conf("ORDER_ENGAGEMENT_ENABLED"))


def engagement_since():
    raw = conf("ORDER_ENGAGEMENT_SINCE")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)


def api_base():
    """Absolute base URL of THIS backend, or '' when not configured safely."""
    base = (conf("ORDERS_API_BASE_URL") or "").rstrip("/")
    return base


# ═════════════════════════════════════════════════════════════════════
#  Email classification
# ═════════════════════════════════════════════════════════════════════
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "msn.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "zoho.com",
}
DISPOSABLE = {
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "throwawaymail.com", "dispostable.com", "maildrop.cc",
    "fakeinbox.com", "mintemail.com", "mohmal.com", "tempail.com", "emailondeck.com",
}
_INSTITUTIONAL_RE = re.compile(r"\.(edu|ac)(\.[a-z]{2,3})?$")


def email_domain(email):
    return (email or "").rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""


def email_class(email):
    d = email_domain(email)
    if not d:
        return "none"
    if d in DISPOSABLE:
        return "disposable"
    if _INSTITUTIONAL_RE.search(d):
        return "institutional"
    if d in FREEMAIL:
        return "freemail"
    return "corporate"


# ═════════════════════════════════════════════════════════════════════
#  Lead scoring
# ═════════════════════════════════════════════════════════════════════
def progress_pct(order):
    steps = len(order.site_info.get("steps") or [])
    if steps <= 1:
        return 0
    return max(0, min(100, round(100 * order.current_step / (steps - 1))))


def score_order(order, engagement=None):
    """Returns (score 0-100, tier, breakdown[list of [label, points]])."""
    from .models import Order  # local: avoids import cycle at module load

    pts = []

    def add(label, n):
        if n:
            pts.append([label, int(n)])

    ecls = email_class(order.email)
    if ecls == "disposable":
        add("Disposable email address", -40)
    elif order.email:
        add("Email captured", 12)
        if ecls == "institutional":
            add("University / institutional email", 8)
        elif ecls == "corporate":
            add("Company email domain", 6)
    if order.phone:
        add("Phone / WhatsApp captured", 15)
    words = len((order.name or "").split())
    add("Full name given" if words >= 2 else "Name given", 6 if words >= 2 else (2 if words else 0))
    if order.service_slug:
        add("Chose a service", 8)

    pct = progress_pct(order)
    add(f"Got {pct}% through the wizard", round(15 * pct / 100))

    dlen = len((order.details or "").strip())
    if dlen >= 200:
        add("Wrote a detailed brief", 12)
    elif dlen >= 40:
        add("Wrote project details", 8)
    if order.topic:
        add("Gave a topic", 3)
    if order.pk and order.files.exists():
        add("Uploaded files", 10)

    d = order.days_left
    if d is not None:
        if d < 0:
            add("Deadline already passed", -10)
        elif d <= 3:
            add("Deadline within 3 days", 12)
        elif d <= 7:
            add("Deadline within a week", 8)
        elif d <= 14:
            add("Deadline within two weeks", 4)

    budget = str((order.extra_answers or {}).get("budget") or "")
    if budget:
        big = any(t in budget for t in ("2,000", "10,000", "5,000", "10k", "5k"))
        add("Stated a budget" + (" (substantial)" if big else ""), 9 if big else 3)

    if order.email and order.pk and Order.objects.filter(
        email__iexact=order.email, submitted_at__isnull=False,
    ).exclude(pk=order.pk).exists():
        add("Returning customer", 10)

    if engagement is not None and engagement.clicks:
        add("Clicked a reminder link", min(engagement.clicks, 2) * 6)

    v = order.visitor
    if v is not None and getattr(v, "request_count", 0) >= 8:
        add("Active session (many page hits)", 4)

    score = max(0, min(100, sum(p for _, p in pts)))
    if score >= conf("ORDER_HOT_SCORE"):
        tier = "hot"
    elif score >= conf("ORDER_WARM_SCORE"):
        tier = "warm"
    else:
        tier = "cold"
    return score, tier, pts


def refresh_score(order, engagement, *, force=False, max_age_hours=6):
    """Recompute + persist when stale. Returns the (possibly updated) engagement."""
    now = timezone.now()
    if not force and engagement.scored_at and now - engagement.scored_at < timedelta(hours=max_age_hours):
        return engagement
    score, tier, breakdown = score_order(order, engagement)
    type(engagement).objects.filter(pk=engagement.pk).update(
        lead_score=score, lead_tier=tier, score_breakdown=breakdown, scored_at=now,
    )
    engagement.lead_score, engagement.lead_tier = score, tier
    engagement.score_breakdown, engagement.scored_at = breakdown, now
    return engagement


# ═════════════════════════════════════════════════════════════════════
#  Timezone inference + quiet hours
# ═════════════════════════════════════════════════════════════════════
COUNTRY_TZ = {
    "KE": "Africa/Nairobi", "UG": "Africa/Kampala", "TZ": "Africa/Dar_es_Salaam", "RW": "Africa/Kigali",
    "BI": "Africa/Bujumbura", "ET": "Africa/Addis_Ababa", "SO": "Africa/Mogadishu", "SS": "Africa/Juba",
    "NG": "Africa/Lagos", "GH": "Africa/Accra", "ZA": "Africa/Johannesburg", "ZM": "Africa/Lusaka",
    "ZW": "Africa/Harare", "MW": "Africa/Blantyre", "BW": "Africa/Gaborone", "NA": "Africa/Windhoek",
    "EG": "Africa/Cairo", "MA": "Africa/Casablanca", "SN": "Africa/Dakar", "CM": "Africa/Douala",
    "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin", "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam", "ES": "Europe/Madrid", "IT": "Europe/Rome", "SE": "Europe/Stockholm",
    "US": "America/New_York", "CA": "America/Toronto", "BR": "America/Sao_Paulo",
    "IN": "Asia/Kolkata", "PK": "Asia/Karachi", "BD": "Asia/Dhaka", "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh", "QA": "Asia/Qatar", "SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur",
    "PH": "Asia/Manila", "CN": "Asia/Shanghai", "JP": "Asia/Tokyo", "AU": "Australia/Sydney",
    "NZ": "Pacific/Auckland",
}
# dial code -> ISO country (longest prefix wins)
DIAL_CODES = {
    "254": "KE", "256": "UG", "255": "TZ", "250": "RW", "257": "BI", "251": "ET", "252": "SO", "211": "SS",
    "234": "NG", "233": "GH", "27": "ZA", "260": "ZM", "263": "ZW", "265": "MW", "267": "BW", "264": "NA",
    "20": "EG", "212": "MA", "221": "SN", "237": "CM", "44": "GB", "353": "IE", "49": "DE", "33": "FR",
    "31": "NL", "34": "ES", "39": "IT", "46": "SE", "1": "US", "55": "BR", "91": "IN", "92": "PK",
    "880": "BD", "971": "AE", "966": "SA", "974": "QA", "65": "SG", "60": "MY", "63": "PH", "86": "CN",
    "81": "JP", "61": "AU", "64": "NZ",
}


def _country_from_name(name):
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) == 2 and name.isalpha():
        return name.upper()
    try:
        import pycountry
        return pycountry.countries.lookup(name).alpha_2
    except Exception:
        return ""


def _country_from_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if not digits or not (phone or "").strip().startswith("+"):
        return ""  # without a leading + the dial code is a guess; don't guess
    for length in (3, 2, 1):
        code = DIAL_CODES.get(digits[:length])
        if code:
            return code
    return ""


def _zone(name):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None


def infer_timezone(order):
    """Stated country > phone dial code > IP geolocation > configured default.
    Falls back to UTC+3 fixed offset if the tz database is unavailable."""
    candidates = [
        _country_from_name(order.country),
        _country_from_phone(order.phone),
        (getattr(order.visitor, "country_code", "") or "").upper() if order.visitor_id else "",
    ]
    for code in candidates:
        zone = _zone(COUNTRY_TZ.get(code, "")) if code else None
        if zone is not None:
            return zone
    return _zone(conf("ORDER_REMINDER_DEFAULT_TZ")) or dt_timezone(timedelta(hours=3))


def next_send_time(candidate, tz, salt=0):
    """Earliest moment >= `candidate` that falls inside the client's waking
    window. A small deterministic jitter (per order) stops every deferred
    reminder from landing on the stroke of 08:00."""
    start_h = int(conf("ORDER_REMINDER_WINDOW_START_HOUR"))
    end_h = int(conf("ORDER_REMINDER_WINDOW_END_HOUR"))
    local = candidate.astimezone(tz)
    if start_h <= local.hour < end_h:
        return candidate
    day = local.date() + timedelta(days=1) if local.hour >= end_h else local.date()
    target = datetime.combine(day, time(start_h, 0), tzinfo=tz) + timedelta(minutes=(int(salt) * 7919) % 45)
    return target.astimezone(dt_timezone.utc)


# ═════════════════════════════════════════════════════════════════════
#  Reminder ladder
# ═════════════════════════════════════════════════════════════════════
def ladder_for(order, tier="warm"):
    """Returns (hours_list, is_urgent). Offsets are hours of silence since
    last activity. Per-site overrides: add `reminder_ladder_hours`,
    `reminder_urgent_ladder_hours` or `reminders_enabled` to a catalog site."""
    site = order.site_info
    if site.get("reminders_enabled") is False or not conf("ORDER_REMINDERS_ENABLED"):
        return [], False
    d = order.days_left
    if d is not None and 0 <= d <= int(conf("ORDER_URGENT_DAYS")):
        hours = site.get("reminder_urgent_ladder_hours") or conf("ORDER_REMINDER_URGENT_LADDER_HOURS")
        return [max(1, int(h)) for h in hours], True
    hours = site.get("reminder_ladder_hours") or conf("ORDER_REMINDER_LADDER_HOURS")
    factor = {"hot": 0.75, "warm": 1.0}.get(tier, 1.5)
    ladder = [max(1, round(int(h) * factor)) for h in hours]
    if tier == "cold" and len(ladder) > 3:
        ladder = ladder[:3]  # don't keep chasing a cold lead for two weeks
    return ladder, False


def ab_variant(order, stage):
    return "A" if (order.pk + stage) % 2 == 0 else "B"


# ═════════════════════════════════════════════════════════════════════
#  Admin-facing summary
# ═════════════════════════════════════════════════════════════════════
def journey_summary(order, engagement=None):
    from .models import OrderEngagement

    eng = engagement or OrderEngagement.objects.filter(order_id=order.pk).first()
    if eng is None:
        return ""
    bits = []
    if eng.lead_score:
        bits.append(f"lead score {eng.lead_score} ({eng.get_lead_tier_display().upper()})")
    bits.append(f"started {timesince(order.created_at)} ago")
    if eng.recovered_by_stage:
        extra = ", clicked the link" if eng.clicks else ""
        bits.append(f"RECOVERED by reminder #{eng.recovered_by_stage}{extra}")
    elif eng.lifetime_reminders:
        bits.append(f"{eng.lifetime_reminders} reminder(s) sent")
    if eng.exit_signal_count:
        bits.append("left the page " + ("once" if eng.exit_signal_count == 1 else f"{eng.exit_signal_count} times"))
    return " · ".join(bits)
