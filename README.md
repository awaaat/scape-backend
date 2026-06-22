# Scape Data Solutions — Backend

Django + DRF backend handling: contact form submissions across all pages,
automatic welcome emails to enquirers, admin notifications, visitor/page-view
tracking, and Brevo contact sync. Database is Supabase Postgres, email is
sent via Brevo SMTP.

Verified working end-to-end before delivery: migrations apply cleanly,
`/api/contact/` creates a Lead, links it to the visitor session, renders and
sends both HTML emails, and `/api/track-visit/` records page views.

## 1. Setup

```bash
cd scape_backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Now edit `.env` and fill in your real values:

- **Database** — use the **Session Pooler** connection details from
  Supabase → Connect → Session Pooler (port `5432`). Do **not** use the
  Transaction Pooler (port `6543`) — it doesn't support Django's persistent
  connections or `migrate` reliably.
- **Email** — your Brevo SMTP login and key from Brevo → SMTP & API → SMTP.
- **BREVO_API_KEY** — from Brevo → SMTP & API → API keys (used only to sync
  contacts, not for sending mail).
- **FRONTEND_ORIGINS** — your React app's URL(s), comma separated.

> **Security note:** the credentials you shared earlier in this chat have
> been typed into a conversation log. Treat them as exposed — rotate the
> Brevo API key, the Brevo SMTP key, and the Supabase DB password before
> going to production, then put the new values only in your local `.env`
> (never commit it; it's already in `.gitignore`).

```bash
python manage.py makemigrations visitors leads
python manage.py migrate
python manage.py createsuperuser
python manage.py test_email --to you@yourdomain.com
python manage.py runserver
```

If `test_email` succeeds you'll get a welcome-style email and an admin
notification email in that inbox — confirms SMTP creds are correct before
you wire up the frontend.

## 2. API

### `POST /api/contact/`
Used by every contact form on the site.

```json
{
  "name": "Jane Doe",
  "email": "jane@example.com",
  "company": "Acme Inc",
  "phone": "+1 555 0100",
  "service": "AI & Machine Learning",
  "message": "We need help with predictive analytics.",
  "page_url": "https://scapedatasolutions.com/services/ai"
}
```
`company`, `phone`, `page_url` are optional. On success (`201`), the lead is
saved, linked to the visitor session, a welcome email goes to the user, and
an admin notification goes to everyone in `ADMIN_NOTIFICATION_EMAILS`. Email
failures never fail the request — they're logged and the form still
succeeds for the visitor.

### `POST /api/track-visit/`
Call this from your React router on every route change to log page views.

```json
{ "url": "https://scapedatasolutions.com/about", "referrer": "", "title": "About" }
```

### `GET /api/health/`
Returns `{"status": "ok"}` — point your uptime monitor at this.

## 3. Frontend integration (React)

```js
// api.js
const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000/api";

export async function submitContactForm(data) {
  const res = await fetch(`${API_BASE}/contact/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...data, page_url: window.location.href }),
  });
  if (!res.ok) throw new Error((await res.json()).message || "Submission failed");
  return res.json();
}

export function trackPageView() {
  fetch(`${API_BASE}/track-visit/`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include", // required so the session cookie ties page views together
    body: JSON.stringify({ url: window.location.href, referrer: document.referrer, title: document.title }),
  }).catch(() => {}); // tracking should never block the UI
}
```

Call `trackPageView()` once on mount and again on every route change (e.g.
inside a `useEffect` keyed on `location.pathname` if you're using React
Router). Use `credentials: "include"` on both calls — that's what lets the
backend tie a contact form submission back to that visitor's earlier page
views.

## 4. Where things live

```
backend/settings.py     — all config, reads from .env
visitors/models.py       — Visitor (one row per session), PageView (one row per page hit)
visitors/middleware.py   — upserts the Visitor record on every API request
leads/models.py          — Lead, linked to Visitor when available
leads/email.py           — welcome email, admin notification, Brevo sync
leads/templates/email/   — the two HTML email templates (table-based, inline-styled for Outlook/Gmail compatibility)
```

## 5. Admin dashboard

`/admin/` — log in with the superuser you created. You get full lead lists
(filterable by service/date, with a `is_processed` checkbox you can tick
inline) and visitor records with their page-view history inline.

## 6. Deploying

```bash
python manage.py collectstatic --noinput
gunicorn backend.wsgi:application --bind 0.0.0.0:8000
```

Set `DEBUG=False` in production `.env` — this automatically turns on HTTPS
redirect, secure cookies, and HSTS (see the bottom of `settings.py`). Make
sure `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` list your real domain.

## 7. Sensible next steps, not built in (scope call)

- **Async email sending (Celery + Redis)** — not added. Email send happens
  synchronously inside the request; if Brevo is slow, the form response is
  slightly slower but still succeeds (DB save happens first, before any
  email attempt). Worth adding once traffic is high enough that this
  matters — adds real infra (a broker) you'd need to run and monitor.
- **reCAPTCHA / hCaptcha** on the contact form — the only spam defense
  right now is the 5/minute per-IP throttle on `/api/contact/`. Add a
  captcha if spam becomes a problem.
- **IP geolocation** for visitor `country` — no external geo API is wired
  up; would need a paid service or a local GeoIP database.
