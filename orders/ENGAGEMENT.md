# Order engagement engine

What each alert is, and when it fires (all go to `ORDER_NOTIFICATION_EMAILS`):

| Alert | Subject starts with | Fires when |
|---|---|---|
| Intent | `[ORDER STARTED · site]` | contact details (name + email) are first captured, finished or not. Once per order; same email within 6h is deduped. Quick one-shot orders skip it. |
| Abandoned | `[ABANDONED ORDER ...]` (existing) | 5 min after the exit beacon (if the front end sends it), otherwise 30 min idle. |
| Submitted | existing new-order subject | order submitted. Now includes a **Journey** line (lead score, "RECOVERED by reminder #N"). |
| Hot lead | `[HOT LEAD PENDING ...]` | score >= 65 and still a draft after 24h. |
| Warm signal | `[WARM SIGNAL ...]` | they clicked a reminder link but did not finish. |
| Digest | `[ORDER DIGEST]` | daily 08:00 Nairobi: last 24h stats + open leads ranked by score. |

## Client reminder ladder
Offsets are hours of silence: 24h, 72h, 7d, 14d (HOT leads x0.75, COLD x1.5 and max 3). Deadline within 3 days
switches to an urgent ladder: 4h, 24h, 48h. Reminders never send during 20:00-08:00 in the client's inferred timezone
(stated country, then phone dial code, then IP, then Africa/Nairobi). Stops on: submit, unsubscribe, bounce/complaint,
passed deadline, a newer draft from the same email (superseded), a submitted order from the same email (converted),
30 days of age, 6 lifetime reminders, 3 reminders per email per week, or "Pause automatic reminders" in the admin.
If the client comes back and leaves again the ladder restarts.

## Front-end exit beacon (optional but recommended)
```js
// call once the order has a token (after POST /api/orders/start/)
const base = "https://<your-backend>/api/orders/" + token;
window.addEventListener("pagehide", () => navigator.sendBeacon(base + "/leave/"));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") navigator.sendBeacon(base + "/return/");
});
```
Loading the draft (`GET /api/orders/<token>/`) also cancels a pending exit signal.

## Brevo bounce/complaint webhook (optional)
Set `ANYMAIL_WEBHOOK_SECRET=user:long-random` on Render, then in Brevo add a transactional webhook to
`https://user:long-random@<backend-host>/anymail/brevo/tracking/` for hard bounce, blocked, spam and unsubscribe.
Soft bounces are ignored on purpose.

## Operations
* `python manage.py run_order_engagement --dry-run` prints every decision (send / wait / stop + reason) and sends nothing.
* `python manage.py run_order_engagement --digest` sends the digest now.
* Admin: Orders > each order has an Engagement inline and reminder log; actions: pause, resume, send next reminder now.
  Do-not-email list: Orders > Email suppressions.
* Only orders created after `ORDER_ENGAGEMENT_SINCE` (set to install time) are managed. Set it earlier to include older drafts.
* Per-site overrides: add `reminder_ladder_hours`, `reminder_urgent_ladder_hours` or `reminders_enabled` to a site in catalog.py.
* With `gunicorn --preload`, prefer Celery beat or cron over `ORDER_ENGINE_INPROCESS`.
