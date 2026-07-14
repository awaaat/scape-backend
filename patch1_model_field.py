"""
Run from ~/scape_backend: python3 patch1_model_field.py
Adds PropertyReport.wallet_applied_kes -- records how much of a partial
wallet balance was earmarked toward a report's price at checkout time.
NOT the same as an actual debit -- the wallet isn't touched until
payment_succeeded confirms the Paystack remainder went through (see
patch3_signals.py), so an abandoned checkout never strands the balance.
"""
path = "property_intel/models.py"
with open(path, "r") as f:
    content = f.read()

old = '''    price_charged_kes = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    is_paid = models.BooleanField(default=False)'''

new = '''    price_charged_kes = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    wallet_applied_kes = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text=(
            "Partial wallet balance earmarked toward this report's price at "
            "checkout time (e.g. KES 36 of a KES 199 report). Only actually "
            "debited from the wallet once the Paystack remainder is "
            "confirmed paid -- see property_intel/signals.py."
        ),
    )
    is_paid = models.BooleanField(default=False)'''

if content.count(old) != 1:
    raise SystemExit("Patch 1 FAILED: anchor not found or not unique -- no changes written.")
with open(path, "w") as f:
    f.write(content.replace(old, new, 1))
print("Patch 1 OK: wallet_applied_kes field added to PropertyReport")
