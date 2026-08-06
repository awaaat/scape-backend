# `contracts` app — client change requests + disputes (Upwork-style)

Answers directly: **no, a client cannot modify or delete a contract.**
That's intentional, matching how Upwork itself works — neither party
can unilaterally rewrite or destroy a live agreement. What clients get
instead, now implemented:

- **Propose a change** — client submits a proposed title/scope/value/
  currency change + reason. Sits as `pending` until an admin approves
  (which applies it through the exact same `record_contract_revision()`
  your negotiation flow already uses — same audit trail either way) or
  rejects it with a note. A client can never apply a change directly.
- **File a dispute** — against the whole contract (not per-milestone,
  per your confirmation), with a category and description. **While a
  dispute is open, ALL milestone invoicing and payment on that contract
  is frozen** — enforced in the one function that starts every Paystack
  checkout (`services.initiate_milestone_payment`), so there's no way
  to route around it from either the admin-invoice or client-pay
  endpoint. An admin resolves it (client favor / provider favor /
  mutual / withdrawn), which immediately unfreezes billing.

Nothing here lets a client edit or delete anything directly — every
mutation still requires an admin decision, exactly like Upwork's
resolution center.

## What files this patch touches

This is an **incremental patch to your existing `contracts` app**
(unlike the brand-new `projects` app) — these are full replacement
files, each containing 100% of your original code plus the additions:

```
contracts/models.py        + ContractChangeRequest, ContractDispute, Contract.has_open_dispute
contracts/serializers.py   + change-request & dispute serializers
contracts/services.py      + request_contract_change, decide_change_request,
                              raise_dispute, resolve_dispute
                            + initiate_milestone_payment() now refuses to run
                              while a dispute is open (the only behavior
                              change to existing code — everything else here
                              is purely additive)
contracts/views.py         + change-request & dispute endpoints, admin + client
contracts/urls.py          + routes for the above
contracts/emails.py        + 4 new notification functions
contracts/admin.py         + read-only inlines/registrations for both new models
contracts/migrations/0002_change_requests_and_disputes.py   (new, incremental)
contracts/templates/email/contract_change_request_submitted.html   (new)
contracts/templates/email/contract_change_request_decided.html     (new)
contracts/templates/email/contract_dispute_raised.html             (new)
contracts/templates/email/contract_dispute_resolved.html           (new)
contracts/tests_change_requests_disputes.py                        (new)
```

`contracts/consumers.py`, `routing.py`, `channels_auth.py`,
`permissions.py`, `apps.py`, `signals.py` are **untouched** — real-time
messaging and access control didn't need to change for any of this.

## Install

1. Replace the 7 existing files above with the versions in this
   folder (diff first if you've since modified any of them yourself).
2. Copy `migrations/0002_change_requests_and_disputes.py` into
   `contracts/migrations/`.
3. Copy the 4 new templates into `contracts/templates/email/`.
4. Copy `tests_change_requests_disputes.py` into `contracts/`.
5. `python manage.py migrate`

No settings/urls/asgi changes needed beyond what your existing
`contracts` wiring already has — the new routes live under the same
`contracts.urls` module already included in `backend/urls.py`.

## API reference

### Client-side
```
GET/POST  /api/contracts/client/<id>/change-requests/
GET/POST  /api/contracts/client/<id>/disputes/
```

### Admin-side (staff only)
```
GET   /api/contracts/<id>/change-requests/
POST  /api/contracts/change-requests/<id>/decide/     {approve: bool, admin_note?}

GET   /api/contracts/<id>/disputes/
GET   /api/contracts/admin/disputes/open/              — triage queue, all contracts
POST  /api/contracts/disputes/<id>/resolve/            {status, resolution_note?}
```

`status` for resolve must be one of: `under_review`,
`resolved_client_favor`, `resolved_provider_favor`, `resolved_mutual`,
`withdrawn`. `under_review` keeps billing frozen; anything else lifts
it immediately.

## Honest scope notes

- A dispute freezes **billing only** (invoicing/checkout) — it does
  NOT auto-pause the message thread, auto-notify a delivery deadline,
  or auto-refund anything already paid. Refunds still go through
  Paystack directly, same as today; this system tracks and enforces
  the freeze, it doesn't move money on its own.
- One open dispute per contract at a time, by design — a second
  concern goes in the resolution note or the message thread, not a
  parallel dispute.
- Change requests are contract-wide (title/scope/value/currency), not
  per-milestone. If you also want clients proposing changes to a
  *specific milestone* (different amount/due date) rather than the
  contract as a whole, that's a reasonable follow-up but a different
  shape — say so and I'll scope it separately.
